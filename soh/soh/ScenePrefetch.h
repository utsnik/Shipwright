#pragma once

#ifdef __WIIU__

#include <stdint.h>

namespace SOH {

extern "C" {
void ScenePrefetch_Initialize();
void ScenePrefetch_Shutdown();
void ScenePrefetch_BeginScene(int16_t sceneId, const char* scenePath, int16_t setupIndex);
void ScenePrefetch_SceneInit(int16_t sceneId);
void ScenePrefetch_Frame();
void ScenePrefetch_NotifyTransition(uint16_t entranceIndex);
void ScenePrefetch_Cancel();
}

} // namespace SOH

#endif
