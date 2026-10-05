#pragma once

// Wii U defaults for packs found in mods/ for the first time: which of the files in Djipi's 3DS Experience and
// Skilar's Art Plus Link (GameBanana 477979) start switched on, and in what order. This is the set that was
// play-tested on the console in October 2026. Packs the user already has in EnabledMods are never touched (except once,
// on the first start with this profile: see gWiiU.ModProfileApplied in mod_menu.cpp; and Djipi's Link, once, when Art
// Plus is switched on for the first time: see ArtPlusArrives), and a
// pack that starts off can be switched on in the mod menu (it is then kept in EnabledMods).
//
// Pure functions on file names (no extension, as the mod menu stores them) so they can be tested on a PC.

#include <algorithm>
#include <string>
#include <vector>

namespace WiiUModProfile {

inline bool StartsWith(const std::string& s, const char* prefix) {
    return s.rfind(prefix, 0) == 0;
}

inline bool IsArtPlus(const std::string& name) {
    return StartsWith(name, "Art Plus - ");
}

inline bool IsDjipi(const std::string& name) {
    return StartsWith(name, "Djipi's 3DE - ");
}

// True when a newly found pack should start switched off.
inline bool StartsDisabled(const std::string& name, bool artPlusPresent) {
    if (!IsDjipi(name) && !IsArtPlus(name)) {
        return false;
    }
    // Optional extras the pack author marks as such (ARIA, Majora chest, 3D objects, full-3D backgrounds, N64 HUD,
    // Crescent Moon, 3DS-style Link textures).
    if (name.find("(OPTIONAL)") != std::string::npos) {
        return true;
    }
    // Art Plus replaces Link; Djipi's own Link textures say to delete them when a custom player model is used.
    if (artPlusPresent && StartsWith(name, "Djipi's 3DE - 02 ")) {
        return true;
    }
    // The 3DS Hyrule Field terrain model is ~85% of Hyrule Field's CPU cost on the Wii U (46 -> 7.6 ms per frame
    // without it); its textures (31) stay on and look right on the original terrain.
    if (StartsWith(name, "Djipi's 3DE - 30 ")) {
        return true;
    }
    // Ice Cavern variant of 22; not part of the tested set.
    if (name == "Djipi's 3DE - 22 Scenes Temples 3DS.Ice") {
        return true;
    }
    return false;
}

// True when this batch switches Art Plus on for the first time: no Art Plus pack was enabled before, and one is
// about to be. Djipi's Link (02) may already be enabled from an earlier start without Art Plus; it is then switched
// off once, here. Later starts see Art Plus already enabled, so a 02 the player switches back on stays on.
inline bool ArtPlusArrives(const std::vector<std::string>& enabledBefore, const std::vector<std::string>& newlyEnabled) {
    const auto anyArtPlus = [](const std::vector<std::string>& names) {
        return std::any_of(names.begin(), names.end(), IsArtPlus);
    };
    return !anyArtPlus(enabledBefore) && anyArtPlus(newlyEnabled);
}

inline bool IsDjipiLink(const std::string& name) {
    return StartsWith(name, "Djipi's 3DE - 02 ");
}

// Load-order group for a batch of newly found packs (lower loads first; later packs win):
// Art Plus, then Djipi's packs by number, then everything else, then zz-* fix packs last.
inline int Rank(const std::string& name) {
    if (IsArtPlus(name)) {
        return 0;
    }
    if (IsDjipi(name)) {
        return 1;
    }
    if (StartsWith(name, "zz-")) {
        return 3;
    }
    return 2;
}

// Orders a batch of new pack names in place (stable within a group, name order otherwise).
inline void SortNew(std::vector<std::string>& names) {
    std::stable_sort(names.begin(), names.end(), [](const std::string& a, const std::string& b) {
        const int ra = Rank(a);
        const int rb = Rank(b);
        return ra != rb ? ra < rb : a < b;
    });
}

} // namespace WiiUModProfile
