#include "ResourceManagerHelpers.h"
#include <libultraship/libultraship.h>
#include "soh/resource/type/Scene.h"
#include <ship/utils/StringHelper.h>
#include "soh/Enhancements/game-interactor/GameInteractor_Hooks.h"
#ifdef __WIIU__
#include "ScenePrefetch.h"
#endif
#include "global.h"
#include "vt.h"
#include <fast/resource/type/Vertex.h>

extern "C" void Play_InitScene(PlayState* play, s32 spawn);
extern "C" void Play_InitEnvironment(PlayState* play, s16 skyboxId);
void OTRPlay_InitScene(PlayState* play, s32 spawn);
s32 OTRScene_ExecuteCommands(PlayState* play, SOH::Scene* scene);

// LUS::OTRResource* OTRPlay_LoadFile(PlayState* play, RomFile* file) {
Ship::IResource* OTRPlay_LoadFile(PlayState* play, const char* fileName) {
    auto res = Ship::Context::GetInstance()->GetResourceManager()->LoadResource(fileName);
    return res.get();
}

extern "C" void OTRPlay_SpawnScene(PlayState* play, s32 sceneId, s32 spawn) {
    SceneTableEntry* scene = &gSceneTable[sceneId];

    scene->unk_13 = 0;
    play->loadedScene = scene;
    play->sceneNum = sceneId;
    play->sceneConfig = scene->config;

    // osSyncPrintf("\nSCENE SIZE %fK\n", (scene->sceneFile.vromEnd - scene->sceneFile.vromStart) / 1024.0f);

    // Scenes considered "dungeon" with a MQ variant
    int16_t inNonSharedScene = (sceneId >= SCENE_DEKU_TREE && sceneId <= SCENE_ICE_CAVERN) ||
                               sceneId == SCENE_GERUDO_TRAINING_GROUND || sceneId == SCENE_INSIDE_GANONS_CASTLE;

    std::string sceneVersion = "shared";
    if (inNonSharedScene) {
        sceneVersion = ResourceMgr_IsGameMasterQuest() ? "mq" : "nonmq";
    }
    std::string scenePath = StringHelper::Sprintf("scenes/%s/%s/%s", sceneVersion.c_str(), scene->sceneFile.fileName,
                                                  scene->sceneFile.fileName);

#ifdef __WIIU__
    // Start capture and resolve any learned manifest before the initial scene entry is requested.
    ScenePrefetch_BeginScene(sceneId, scenePath.c_str(), gSaveContext.sceneSetupIndex);
#endif

    play->sceneSegment = OTRPlay_LoadFile(play, scenePath.c_str());

    // Failed to load scene... default to doodongs cavern
    if (play->sceneSegment == nullptr) {
        lusprintf(__FILE__, __LINE__, 2, "Unable to load scene %s... Defaulting to Doodong's Cavern!\n",
                  scenePath.c_str());
        OTRPlay_SpawnScene(play, 0x01, 0);
        return;
    }

    scene->unk_13 = 0;

    // gSegments[2] = VIRTUAL_TO_PHYSICAL(play->sceneSegment);

    OTRPlay_InitScene(play, spawn);
    auto roomSize = func_80096FE8(play, &play->roomCtx);

    osSyncPrintf("ROOM SIZE=%fK\n", roomSize / 1024.0f);

#ifdef __WIIU__
    ScenePrefetch_SceneInit(play->sceneNum);
#endif
    GameInteractor_ExecuteOnSceneInit(play->sceneNum);
    SPDLOG_INFO("Scene Init - sceneNum: {0:#x}, entranceIndex: {1:#x}", play->sceneNum, gSaveContext.entranceIndex);
}

void OTRPlay_InitScene(PlayState* play, s32 spawn) {
    play->curSpawn = spawn;
    play->linkActorEntry = nullptr;
    play->unk_11DFC = nullptr;
    play->setupEntranceList = nullptr;
    play->setupExitList = nullptr;
    play->cUpElfMsgs = nullptr;
    play->setupPathList = nullptr;
    play->numSetupActors = 0;
    Object_InitBank(play, &play->objectCtx);
    LightContext_Init(play, &play->lightCtx);
    TransitionActor_InitContext(&play->state, &play->transiActorCtx);
    func_80096FD4(play, &play->roomCtx.curRoom);
    YREG(15) = 0;
    gSaveContext.worldMapArea = 0;
    OTRScene_ExecuteCommands(play, (SOH::Scene*)play->sceneSegment);

    GameInteractor_ExecuteAfterSceneCommands(play->sceneNum);
    Play_InitEnvironment(play, play->skyboxId);
    /* auto data = static_cast<LUS::Vertex*>(Ship::Context::GetInstance()
                                               ->GetResourceManager()
                                               ->ResourceLoad("object_link_child\\object_link_childVtx_01FE08")
                                               .get());

    auto data2 = ResourceMgr_LoadVtxByCRC(0x68d4ea06044e228f);*/

    volatile int a = 0;
}

#ifdef __WIIU__
// Game-state reads for ScenePrefetch.cpp, which cannot include SoH's game headers (libultra/Cafe type clash).
extern "C" int16_t ScenePrefetch_GameSceneForEntrance(uint16_t entranceIndex) {
    if (entranceIndex >= ENTR_MAX) {
        return -1;
    }
    const int16_t scene = gEntranceTable[entranceIndex].scene;
    return (scene >= 0 && scene < SCENE_ID_MAX) ? scene : -1;
}

extern "C" int16_t ScenePrefetch_GameSetupIndex(void) {
    return gSaveContext.sceneSetupIndex;
}
#endif
