#include "SohModals.h"
#include <imgui.h>
#include <vector>
#include <string>
#include <cstdio>
#if defined(__WIIU__) && (WIIU_DIAGNOSTICS || WIIU_AUTOACCEPT_TEST)
#include <spdlog/spdlog.h>
#endif
#include <libultraship/bridge.h>
#include <libultraship/libultraship.h>
#include "UIWidgets.hpp"
#include "SohGui.hpp"
#include "soh/OTRGlobals.h"
#include "z64.h"

extern "C" PlayState* gPlayState;
struct SohModal {
    std::string title_;
    std::string message_;
    std::string button1_;
    std::string button2_;
    std::function<void()> button1callback_;
    std::function<void()> button2callback_;
};
std::vector<SohModal> modals;

bool closePopup = false;

#if defined(__WIIU__) && (WIIU_DIAGNOSTICS || WIIU_AUTOACCEPT_TEST)
bool AutoAcceptPopupEnabled() {
    static const bool enabled = []() {
        FILE* marker = fopen("autoaccept-popup.txt", "r");
        if (marker != nullptr) {
            fclose(marker);
            return true;
        }
        return false;
    }();
    return enabled;
}
std::string autoAcceptTitle;
double autoAcceptStartedAt = 0.0;
#endif

void SohModalWindow::Draw() {
    if (!IsVisible()) {
        return;
    }
    DrawElement();
    // Sync up the IsVisible flag if it was changed by ImGui
    SyncVisibilityConsoleVariable();
}

void SohModalWindow::DrawElement() {
    if (modals.size() > 0) {
        SohModal curModal = modals.at(0);
        if (!ImGui::IsPopupOpen(curModal.title_.c_str())) {
            ImGui::OpenPopup(curModal.title_.c_str());
        }
        if (closePopup) {
            ImGui::CloseCurrentPopup();
            modals.erase(modals.begin());
            closePopup = false;
#if defined(__WIIU__) && (WIIU_DIAGNOSTICS || WIIU_AUTOACCEPT_TEST)
            autoAcceptTitle.clear();
#endif
        }
        ImGui::SetNextWindowPos(ImGui::GetMainViewport()->GetCenter(), ImGuiCond_Always, ImVec2(0.5f, 0.5f));
        if (ImGui::BeginPopupModal(curModal.title_.c_str(), NULL,
                                   ImGuiWindowFlags_AlwaysAutoResize | ImGuiWindowFlags_NoResize |
                                       ImGuiWindowFlags_NoMove | ImGuiWindowFlags_NoScrollbar |
                                       ImGuiWindowFlags_NoSavedSettings)) {
#if defined(__WIIU__) && (WIIU_DIAGNOSTICS || WIIU_AUTOACCEPT_TEST)
            if (autoAcceptTitle != curModal.title_) {
                autoAcceptTitle = curModal.title_;
                autoAcceptStartedAt = ImGui::GetTime();
            }
#endif
            ImGui::Text("%s", curModal.message_.c_str());
            bool accepted = false;
            auto acceptPopup = [&](const std::function<void()>& callback) {
                if (callback != nullptr) {
                    callback();
                }
                ImGui::CloseCurrentPopup();
                modals.erase(modals.begin());
                accepted = true;
#if defined(__WIIU__) && (WIIU_DIAGNOSTICS || WIIU_AUTOACCEPT_TEST)
                autoAcceptTitle.clear();
#endif
            };
            UIWidgets::PushStyleButton(THEME_COLOR);
            if (ImGui::Button(curModal.button1_.c_str())) {
                acceptPopup(curModal.button1callback_);
            }
            UIWidgets::PopStyleButton();
            if (curModal.button2_ != "") {
                ImGui::SameLine();
                UIWidgets::PushStyleButton(THEME_COLOR);
                if (ImGui::Button(curModal.button2_.c_str())) {
                    acceptPopup(curModal.button2callback_);
                }
                UIWidgets::PopStyleButton();
            }
#if defined(__WIIU__) && (WIIU_DIAGNOSTICS || WIIU_AUTOACCEPT_TEST)
            if (!accepted && AutoAcceptPopupEnabled() && ImGui::GetTime() - autoAcceptStartedAt >= 5.0) {
                SPDLOG_INFO("AUTOACCEPT popup {}", curModal.title_);
                acceptPopup(curModal.button1callback_);
            }
#endif
            ImGui::EndPopup();
        }
    } else {
#if defined(__WIIU__) && (WIIU_DIAGNOSTICS || WIIU_AUTOACCEPT_TEST)
        autoAcceptTitle.clear();
#endif
    }
}

void SohModalWindow::RegisterPopup(std::string title, std::string message, std::string button1, std::string button2,
                                   std::function<void()> button1callback, std::function<void()> button2callback) {
    modals.push_back({ title, message, button1, button2, button1callback, button2callback });
}

size_t SohModalWindow::PopupsQueued() {
    return modals.size();
}

bool SohModalWindow::IsPopupOpen(std::string title) {
    return !modals.empty() && modals.at(0).title_ == title;
}

void SohModalWindow::DismissPopup() {
    closePopup = true;
}
