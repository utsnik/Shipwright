#ifdef __WIIU__

#include "ScenePrefetch.h"

// Only Cafe and libultraship headers here: SoH's game headers bring libultra's OSThread/OSTime, which clash
// with coreinit/thread.h. The few game values needed come through C helpers in z_play_otr.cpp.
extern "C" uint32_t ResourceMgr_IsSceneMasterQuest(int16_t sceneNum);
extern "C" int16_t ScenePrefetch_GameSceneForEntrance(uint16_t entranceIndex);
extern "C" int16_t ScenePrefetch_GameSetupIndex(void);
extern "C" bool ScenePrefetch_GameScenePath(int16_t sceneId, char* out, unsigned long outSize);
#include <ship/Context.h>
#include <ship/config/ConsoleVariable.h>
#include <libultraship/bridge/consolevariablebridge.h>
#include <ship/resource/ResourceManager.h>
#include <ship/resource/archive/Archive.h>
#include <ship/resource/archive/ArchiveManager.h>
#include <ship/resource/archive/O2rArchive.h>
#include <spdlog/spdlog.h>

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cstdio>
#include <deque>
#include <filesystem>
#include <fstream>
#include <memory>
#include <sstream>
#include <string>
#include <unordered_map>
#include <utility>
#include <vector>

#include <coreinit/fastmutex.h>
#include <coreinit/thread.h>
#include <coreinit/time.h>

namespace SOH {
namespace {

constexpr uint32_t kManifestVersion = 1;
constexpr size_t kMaxManifestEntries = 256;
constexpr size_t kMaxManifestBytes = 64 * 1024;
constexpr uint32_t kCaptureAfterSceneInitMs = 4000;
constexpr uint32_t kWorkerPriority = 24;
constexpr size_t kWorkerStackBytes = 16 * 1024;

extern "C" void wiiu_get_perf_big_heap(uint32_t* free_bytes, uint32_t* largest_free_bytes);

using Clock = std::chrono::steady_clock;

struct LearnedEntry {
    std::string path;
    std::string archivePath;
    size_t compressedSize = 0;
    size_t uncompressedSize = 0;
    uint32_t count = 0;
    uint32_t firstUse = 0;
};

struct Manifest {
    std::string key;
    std::string signature;
    std::vector<LearnedEntry> entries;
};

struct WarmEntry {
    std::shared_ptr<Ship::O2rArchive> archive;
    std::string path;
    size_t compressedSize = 0;
    size_t uncompressedSize = 0;
    uint32_t firstUse = 0;
};

struct WarmJob {
    uint64_t generation = 0;
    std::string sceneKey;
    bool manifestHit = false;
    size_t budget = 0;
    std::vector<WarmEntry> entries;
};

struct WriteJob {
    uint64_t generation = 0;
    std::string sceneKey;
    std::string signature;
    std::string manifestPath;
    std::vector<LearnedEntry> entries;
};

struct Job {
    enum class Type { Warm, Write } type = Type::Warm;
    WarmJob warm;
    WriteJob write;
};

struct Capture {
    bool active = false;
    bool sceneInitSeen = false;
    std::string sceneKey;
    std::string signature;
    std::string manifestPath;
    Clock::time_point deadline{};
    std::vector<LearnedEntry> entries;
    std::unordered_map<std::string, size_t> indexes;
};

struct FastMutexGuard {
    explicit FastMutexGuard(OSFastMutex& mutex) : mMutex(&mutex) {
        OSFastMutex_Lock(mMutex);
    }
    ~FastMutexGuard() {
        OSFastMutex_Unlock(mMutex);
    }
    OSFastMutex* mMutex;
};

uint64_t Fnv1a(const std::string& value) {
    uint64_t hash = 1469598103934665603ull;
    for (const unsigned char byte : value) {
        hash ^= byte;
        hash *= 1099511628211ull;
    }
    return hash;
}

std::string ManifestFileName(const std::string& key) {
    std::ostringstream stream;
    stream << "scene_" << std::hex << Fnv1a(key) << ".spf";
    return stream.str();
}

std::string ScenePath(int16_t sceneId) {
    char path[160];
    return ScenePrefetch_GameScenePath(sceneId, path, sizeof(path)) ? std::string(path) : std::string();
}

std::string SceneKey(int16_t sceneId, const std::string& scenePath, int16_t setupIndex) {
    std::ostringstream stream;
    stream << scenePath << "|setup=" << setupIndex << "|mq=" << ResourceMgr_IsSceneMasterQuest(sceneId)
           << "|alt=" << Ship::Context::GetInstance()->GetResourceManager()->IsAltAssetsEnabled();
    return stream.str();
}

std::string ArchiveSignature() {
    std::ostringstream stream;
    const auto archives = Ship::Context::GetInstance()->GetResourceManager()->GetArchiveManager()->GetArchives();
    for (const auto& archive : *archives) {
        const std::filesystem::path path(archive->GetPath());
        std::error_code error;
        const auto size = std::filesystem::file_size(path, error);
        const auto time = std::filesystem::last_write_time(path, error).time_since_epoch().count();
        stream << archive->GetPath() << '|' << size << '|' << time << '|' << archive->GetGameVersion() << ';';
        if (stream.tellp() >= static_cast<std::streamoff>(kMaxManifestBytes / 2)) {
            break;
        }
    }
    return stream.str();
}

bool ParseManifest(const std::string& path, const std::string& key, const std::string& signature, Manifest& manifest) {
    std::ifstream file(path, std::ios::in | std::ios::binary);
    if (!file) {
        return false;
    }
    file.seekg(0, std::ios::end);
    const auto length = file.tellg();
    if (length <= 0 || length > static_cast<std::streamoff>(kMaxManifestBytes)) {
        return false;
    }
    file.seekg(0);

    std::string line;
    if (!std::getline(file, line) || line != "SCENEPREFETCH " + std::to_string(kManifestVersion)) {
        return false;
    }
    if (!std::getline(file, line) || !line.starts_with("key=")) {
        return false;
    }
    manifest.key = line.substr(4);
    if (!std::getline(file, line) || !line.starts_with("signature=")) {
        return false;
    }
    manifest.signature = line.substr(10);
    if (manifest.key != key || manifest.signature != signature) {
        return false;
    }

    while (std::getline(file, line)) {
        if (!line.starts_with("entry\t") || manifest.entries.size() >= kMaxManifestEntries) {
            return false;
        }
        std::vector<std::string> fields;
        std::stringstream fieldsStream(line.substr(6));
        std::string field;
        while (std::getline(fieldsStream, field, '\t')) {
            fields.push_back(field);
        }
        if (fields.size() != 6) {
            return false;
        }
        try {
            LearnedEntry entry;
            entry.path = fields[0];
            entry.archivePath = fields[1];
            entry.compressedSize = std::stoull(fields[2]);
            entry.uncompressedSize = std::stoull(fields[3]);
            entry.count = std::stoul(fields[4]);
            entry.firstUse = std::stoul(fields[5]);
            if (entry.path.empty() || entry.archivePath.empty() || entry.compressedSize == 0 ||
                entry.uncompressedSize == 0 || entry.compressedSize > 4 * 1024 * 1024 ||
                entry.uncompressedSize > 4 * 1024 * 1024) {
                return false;
            }
            manifest.entries.push_back(std::move(entry));
        } catch (...) {
            return false;
        }
    }
    return !manifest.entries.empty();
}

void WriteManifest(const WriteJob& job) {
    const std::filesystem::path path(job.manifestPath);
    std::error_code error;
    std::filesystem::create_directories(path.parent_path(), error);
    const auto temporaryPath = path.string() + ".tmp";
    std::ofstream file(temporaryPath, std::ios::out | std::ios::binary | std::ios::trunc);
    if (!file) {
        SPDLOG_WARN("SCENEPREFETCH write failed scene={}", job.sceneKey);
        return;
    }
    file << "SCENEPREFETCH " << kManifestVersion << '\n';
    file << "key=" << job.sceneKey << '\n';
    file << "signature=" << job.signature;
    if (!job.signature.empty() && job.signature.back() != '\n') {
        file << '\n';
    }
    size_t entryCount = 0;
    for (const auto& entry : job.entries) {
        if (entryCount >= kMaxManifestEntries) {
            break;
        }
        std::ostringstream line;
        line << "entry\t" << entry.path << '\t' << entry.archivePath << '\t' << entry.compressedSize << '\t'
             << entry.uncompressedSize << '\t' << entry.count << '\t' << entry.firstUse << '\n';
        const auto position = file.tellp();
        if (position < 0 || position + static_cast<std::streamoff>(line.str().size()) >
                                static_cast<std::streamoff>(kMaxManifestBytes)) {
            break;
        }
        file << line.str();
        entryCount++;
    }
    file.close();
    if (!file) {
        std::remove(temporaryPath.c_str());
        return;
    }
    std::filesystem::rename(temporaryPath, path, error);
    if (error) {
        std::filesystem::remove(path, error);
        error.clear();
        std::filesystem::rename(temporaryPath, path, error);
    }
    if (error) {
        std::filesystem::remove(temporaryPath, error);
    }
}

class Coordinator {
  public:
    void Initialize() {
        if (mInitialized) {
            return;
        }
        OSFastMutex_Init(&mQueueMutex, "ScenePrefetchQueue");
        OSFastMutex_Init(&mCaptureMutex, "ScenePrefetchCapture");
        mInitialized = true;
        auto resourceManager = Ship::Context::GetInstance()->GetResourceManager();
        resourceManager->SetFileLoadObserver([this](const std::string& path, std::shared_ptr<Ship::Archive> archive) {
            Record(path, std::move(archive));
        });
        if (OSCreateThread(&mThread, WorkerMain, 0, reinterpret_cast<char*>(this), mStack + sizeof(mStack), sizeof(mStack), kWorkerPriority,
                           OS_THREAD_ATTRIB_AFFINITY_CPU0)) {
            OSSetThreadName(&mThread, "Scene prefetch");
            mThreadStarted = true;
            OSResumeThread(&mThread);
        } else {
            SPDLOG_WARN("SCENEPREFETCH worker thread creation failed");
        }
    }

    void Shutdown() {
        if (!mInitialized) {
            return;
        }
        mGeneration.fetch_add(1, std::memory_order_acq_rel);
        mStopping.store(true, std::memory_order_release);
        if (mThreadStarted) {
            OSJoinThread(&mThread, nullptr);
            mThreadStarted = false;
        }
        auto resourceManager = Ship::Context::GetInstance()->GetResourceManager();
        resourceManager->SetFileLoadObserver(nullptr);
        mInitialized = false;
    }

    void Cancel() {
        if (!mInitialized) {
            return;
        }
        mGeneration.fetch_add(1, std::memory_order_acq_rel);
        {
            const FastMutexGuard lock(mQueueMutex);
            mJobs.clear();
        }
        {
            const FastMutexGuard lock(mCaptureMutex);
            mCapture.active = false;
        }
    }

    void BeginScene(int16_t sceneId, const char* scenePath, int16_t setupIndex) {
        if (!mInitialized || !Enabled()) {
            Cancel();
            return;
        }
        const std::string path(scenePath);
        const std::string key = SceneKey(sceneId, path, setupIndex);
        const std::string signature = ArchiveSignature();
        const bool sameTransition = mPendingKey == key && mPendingSignature == signature;
        if (!sameTransition) {
            mGeneration.fetch_add(1, std::memory_order_acq_rel);
            {
                const FastMutexGuard lock(mQueueMutex);
                mJobs.clear();
            }
        }
        mPendingKey.clear();
        mPendingSignature.clear();
        mLastTransition = UINT16_MAX;

        const std::string manifestPath = ManifestPath(key);
        {
            const FastMutexGuard lock(mCaptureMutex);
            mCapture = {};
            mCapture.active = true;
            mCapture.sceneKey = key;
            mCapture.signature = signature;
            mCapture.manifestPath = manifestPath;
            mCapture.deadline = Clock::now() + std::chrono::seconds(5);
        }
        if (!sameTransition) {
            QueueManifest(key, signature, manifestPath, mGeneration.load(std::memory_order_acquire));
        }
    }

    void SceneInit(int16_t sceneId) {
        (void)sceneId;
        if (!Enabled()) {
            return;
        }
        const FastMutexGuard lock(mCaptureMutex);
        if (mCapture.active) {
            mCapture.sceneInitSeen = true;
            mCapture.deadline = Clock::now() + std::chrono::milliseconds(kCaptureAfterSceneInitMs);
        }
    }

    void Frame() {
        if (!Enabled()) {
            return;
        }
        WriteJob job;
        {
            const FastMutexGuard lock(mCaptureMutex);
            if (!mCapture.active || Clock::now() < mCapture.deadline) {
                return;
            }
            mCapture.active = false;
            job.generation = mGeneration.load(std::memory_order_acquire);
            job.sceneKey = mCapture.sceneKey;
            job.signature = mCapture.signature;
            job.manifestPath = mCapture.manifestPath;
            job.entries = std::move(mCapture.entries);
        }
        if (!job.entries.empty()) {
            Job queueJob;
            queueJob.type = Job::Type::Write;
            queueJob.write = std::move(job);
            Enqueue(std::move(queueJob));
        }
    }

    void Transition(uint16_t entranceIndex) {
        if (!mInitialized || !Enabled() || mLastTransition == entranceIndex) {
            return;
        }
        mLastTransition = entranceIndex;
        const int16_t sceneId = ScenePrefetch_GameSceneForEntrance(entranceIndex);
        if (sceneId < 0) { // the helper returns -1 for an out-of-range entrance or scene
            return;
        }
        const std::string path = ScenePath(sceneId);
        const std::string key = SceneKey(sceneId, path, ScenePrefetch_GameSetupIndex());
        const std::string signature = ArchiveSignature();
        mGeneration.fetch_add(1, std::memory_order_acq_rel);
        {
            const FastMutexGuard lock(mQueueMutex);
            mJobs.clear();
        }
        mPendingKey = key;
        mPendingSignature = signature;
        QueueManifest(key, signature, ManifestPath(key), mGeneration.load(std::memory_order_acquire));
    }

    void Record(const std::string& path, std::shared_ptr<Ship::Archive> archive) {
        if (!Enabled() || archive == nullptr) {
            return;
        }
        auto o2rArchive = std::dynamic_pointer_cast<Ship::O2rArchive>(archive);
        if (o2rArchive == nullptr) {
            return;
        }
        size_t compressedSize = 0;
        size_t uncompressedSize = 0;
        if (!o2rArchive->GetEntryInfo(path, compressedSize, uncompressedSize)) {
            return;
        }
        const FastMutexGuard lock(mCaptureMutex);
        if (!mCapture.active || Clock::now() >= mCapture.deadline || mCapture.entries.size() >= kMaxManifestEntries) {
            return;
        }
        const std::string identity = o2rArchive->GetPath() + "\n" + path;
        const auto existing = mCapture.indexes.find(identity);
        if (existing != mCapture.indexes.end()) {
            mCapture.entries[existing->second].count++;
            return;
        }
        const size_t index = mCapture.entries.size();
        mCapture.indexes.emplace(identity, index);
        mCapture.entries.push_back({ path, o2rArchive->GetPath(), compressedSize, uncompressedSize, 1,
                                     static_cast<uint32_t>(index) });
    }

  private:
    bool Enabled() const {
        return CVarGetInteger("gWiiU.ScenePrefetch", 0) != 0;
    }

    std::string ManifestPath(const std::string& key) const {
        return Ship::Context::GetPathRelativeToAppDirectory("ScenePrefetch/" + ManifestFileName(key));
    }

    std::vector<WarmEntry> ResolveEntries(const Manifest& manifest) {
        std::unordered_map<std::string, std::shared_ptr<Ship::O2rArchive>> archives;
        const auto archiveList = Ship::Context::GetInstance()->GetResourceManager()->GetArchiveManager()->GetArchives();
        for (const auto& archive : *archiveList) {
            if (auto o2r = std::dynamic_pointer_cast<Ship::O2rArchive>(archive)) {
                archives.emplace(archive->GetPath(), std::move(o2r));
            }
        }
        std::vector<WarmEntry> result;
        result.reserve(manifest.entries.size());
        for (const auto& entry : manifest.entries) {
            const auto archive = archives.find(entry.archivePath);
            if (archive == archives.end()) {
                continue;
            }
            result.push_back({ archive->second, entry.path, entry.compressedSize, entry.uncompressedSize,
                               entry.firstUse });
        }
        std::stable_sort(result.begin(), result.end(), [](const WarmEntry& lhs, const WarmEntry& rhs) {
            return lhs.firstUse < rhs.firstUse;
        });
        return result;
    }

    void QueueManifest(const std::string& key, const std::string& signature, const std::string& manifestPath,
                       uint64_t generation) {
        Manifest manifest;
        const bool hit = ParseManifest(manifestPath, key, signature, manifest);
        Job job;
        job.type = Job::Type::Warm;
        job.warm.generation = generation;
        job.warm.sceneKey = key;
        job.warm.manifestHit = hit;
        job.warm.budget = static_cast<size_t>(std::max(0, CVarGetInteger("gWiiU.ScenePrefetchBudgetKB", 1024))) * 1024;
        if (hit) {
            job.warm.entries = ResolveEntries(manifest);
        }
        Enqueue(std::move(job));
    }

    void Enqueue(Job job) {
        const FastMutexGuard lock(mQueueMutex);
        mJobs.push_back(std::move(job));
    }

    bool IsCurrent(uint64_t generation) const {
        return !mStopping.load(std::memory_order_acquire) &&
               generation == mGeneration.load(std::memory_order_acquire);
    }

    void RunWarm(const WarmJob& job) {
        if (!job.manifestHit) {
            uint32_t freeBytes = 0;
            uint32_t largest = 0;
            wiiu_get_perf_big_heap(&freeBytes, &largest);
            (void)freeBytes;
            SPDLOG_INFO("SCENEPREFETCH scene={} manifest=miss bigLargestKB={}", job.sceneKey, largest / 1024);
            return;
        }
        size_t requestedBytes = 0;
        size_t warmedBytes = 0;
        size_t skippedBytes = 0;
        size_t warmedEntries = 0;
        for (const auto& entry : job.entries) {
            if (!IsCurrent(job.generation)) {
                SPDLOG_INFO("SCENEPREFETCH scene={} cancelled warmedKB={}", job.sceneKey, warmedBytes / 1024);
                return;
            }
            requestedBytes += entry.compressedSize;
            const size_t cachedBytes = Ship::O2rArchive::GetScenePrefetchBytes();
            if (cachedBytes >= job.budget) {
                skippedBytes += entry.compressedSize;
                continue;
            }
            size_t storedBytes = 0;
            size_t compressedSize = 0;
            size_t uncompressedSize = 0;
            if (!entry.archive->GetEntryInfo(entry.path, compressedSize, uncompressedSize) ||
                compressedSize != entry.compressedSize || uncompressedSize != entry.uncompressedSize) {
                skippedBytes += entry.compressedSize;
            } else if (entry.archive->PrefetchFile(entry.path, job.budget - cachedBytes, storedBytes)) {
                warmedBytes += storedBytes;
                warmedEntries++;
            } else {
                skippedBytes += entry.compressedSize;
            }
            OSYieldThread();
        }
        uint32_t freeBytes = 0;
        uint32_t largest = 0;
        wiiu_get_perf_big_heap(&freeBytes, &largest);
        SPDLOG_INFO("SCENEPREFETCH scene={} manifest=hit requestedKB={} warmedKB={} skippedKB={} entries={} bigLargestKB={} ",
                    job.sceneKey, requestedBytes / 1024, warmedBytes / 1024, skippedBytes / 1024, warmedEntries,
                    largest / 1024);
    }

    static int WorkerMain(int, const char**);

    OSFastMutex mQueueMutex{};
    OSFastMutex mCaptureMutex{};
    std::deque<Job> mJobs;
    Capture mCapture;
    std::atomic<uint32_t> mGeneration{ 0 }; // 32-bit: devkitPPC has no 64-bit atomics
    std::atomic<bool> mStopping{ false };
    OSThread mThread{};
    alignas(16) uint8_t mStack[kWorkerStackBytes]{};
    bool mInitialized = false;
    bool mThreadStarted = false;
    uint16_t mLastTransition = UINT16_MAX;
    std::string mPendingKey;
    std::string mPendingSignature;

    friend void ScenePrefetch_Initialize();
    friend void ScenePrefetch_Shutdown();
    friend void ScenePrefetch_BeginScene(int16_t, const char*, int16_t);
    friend void ScenePrefetch_SceneInit(int16_t);
    friend void ScenePrefetch_Frame();
    friend void ScenePrefetch_NotifyTransition(uint16_t);
    friend void ScenePrefetch_Cancel();
};

int Coordinator::WorkerMain(int, const char** arg) {
    auto* coordinator = reinterpret_cast<Coordinator*>(const_cast<char*>(reinterpret_cast<const char*>(arg)));
    while (!coordinator->mStopping.load(std::memory_order_acquire)) {
        Job job;
        bool haveJob = false;
        {
            const FastMutexGuard lock(coordinator->mQueueMutex);
            if (!coordinator->mJobs.empty()) {
                job = std::move(coordinator->mJobs.front());
                coordinator->mJobs.pop_front();
                haveJob = true;
            }
        }
        if (!haveJob) {
            OSSleepTicks(OSMillisecondsToTicks(10));
            continue;
        }
        if (!coordinator->IsCurrent(job.type == Job::Type::Warm ? job.warm.generation : job.write.generation)) {
            continue;
        }
        if (job.type == Job::Type::Warm) {
            coordinator->RunWarm(job.warm);
        } else {
            WriteManifest(job.write);
        }
    }
    return 0;
}

Coordinator& GetCoordinator() {
    static Coordinator coordinator;
    return coordinator;
}

} // namespace

} // namespace SOH

using namespace SOH;
extern "C" {
void ScenePrefetch_Initialize() {
    GetCoordinator().Initialize();
}

void ScenePrefetch_Shutdown() {
    GetCoordinator().Shutdown();
}

void ScenePrefetch_BeginScene(int16_t sceneId, const char* scenePath, int16_t setupIndex) {
    GetCoordinator().BeginScene(sceneId, scenePath, setupIndex);
}

void ScenePrefetch_SceneInit(int16_t sceneId) {
    GetCoordinator().SceneInit(sceneId);
}

void ScenePrefetch_Frame() {
    GetCoordinator().Frame();
}

void ScenePrefetch_NotifyTransition(uint16_t entranceIndex) {
    GetCoordinator().Transition(entranceIndex);
}

void ScenePrefetch_Cancel() {
    GetCoordinator().Cancel();
}

} // extern "C"


#endif // __WIIU__
