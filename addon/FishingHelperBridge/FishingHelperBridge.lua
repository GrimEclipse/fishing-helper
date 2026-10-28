local ADDON = ...
local frame = CreateFrame("Frame")
local active = false
local recentCast = 0
local recentFailure = 0
local lastCastGUID = nil

local beacon = CreateFrame("Frame", nil, UIParent)
beacon:SetSize(18, 18)
beacon:SetPoint("TOPLEFT", UIParent, "TOPLEFT", 2, -2)
beacon:SetFrameStrata("TOOLTIP")
local bg = beacon:CreateTexture(nil, "OVERLAY")
bg:SetAllPoints()
bg:SetColorTexture(0, 0.8, 0.15, 1)
beacon:Hide()

local function record(kind, payload)
    if not active or not FishingHelperBridgeDB then return end
    local events = FishingHelperBridgeDB.events
    local clean = tostring(payload or ""):gsub('["\\\r\n]', " ")
    events[#events + 1] = "FH1|" .. time() .. "|" .. kind .. "|" .. clean
    if #events > 3000 then table.remove(events, 1) end
end

local function fishingSpell(spellID)
    if not spellID then return false end
    local info = C_Spell and C_Spell.GetSpellInfo and C_Spell.GetSpellInfo(spellID)
    local name = info and info.name or ""
    return name:find("钓鱼", 1, true) or name:find("抛竿", 1, true)
        or name:lower():find("fishing", 1, true)
end

local function setCombat(inCombat)
    bg:SetColorTexture(inCombat and 0.95 or 0, inCombat and 0.08 or 0.8, 0.15, 1)
    record(inCombat and "combat_on" or "combat_off")
end

local function toggle(value)
    active = value
    FishingHelperBridgeDB.active = value
    if value then
        record("start")
        frame:RegisterEvent("UNIT_SPELLCAST_START")
        frame:RegisterEvent("UNIT_SPELLCAST_SUCCEEDED")
        frame:RegisterEvent("UNIT_SPELLCAST_FAILED")
        frame:RegisterEvent("UI_ERROR_MESSAGE")
        frame:RegisterEvent("CHAT_MSG_LOOT")
        frame:RegisterEvent("PLAYER_REGEN_DISABLED")
        frame:RegisterEvent("PLAYER_REGEN_ENABLED")
        beacon:Show()
        setCombat(UnitAffectingCombat("player"))
    else
        frame:UnregisterEvent("UNIT_SPELLCAST_START")
        frame:UnregisterEvent("UNIT_SPELLCAST_SUCCEEDED")
        frame:UnregisterEvent("UNIT_SPELLCAST_FAILED")
        frame:UnregisterEvent("UI_ERROR_MESSAGE")
        frame:UnregisterEvent("CHAT_MSG_LOOT")
        frame:UnregisterEvent("PLAYER_REGEN_DISABLED")
        frame:UnregisterEvent("PLAYER_REGEN_ENABLED")
        beacon:Hide()
    end
    print("FishingHelper Bridge: " .. (value and "记录已开启" or "记录已停止"))
end

SLASH_FISHINGHELPERBRIDGE1 = "/fh"
SlashCmdList.FISHINGHELPERBRIDGE = function(command)
    command = strlower(strtrim(command or ""))
    if command == "start" or command == "on" then
        toggle(true)
    elseif command == "stop" or command == "off" then
        toggle(false)
    elseif command == "status" then
        print("FishingHelper Bridge: " .. (active and "记录中" or "未记录") .. ", 事件 " .. #FishingHelperBridgeDB.events)
    else
        print("FishingHelper Bridge: /fh start | /fh stop | /fh status")
    end
end

frame:RegisterEvent("ADDON_LOADED")
frame:SetScript("OnEvent", function(_, event, ...)
    if event == "ADDON_LOADED" then
        if ... ~= ADDON then return end
        FishingHelperBridgeDB = FishingHelperBridgeDB or {events = {}}
        FishingHelperBridgeDB.events = FishingHelperBridgeDB.events or {}
        FishingHelperBridgeDB.active = false
        return
    end
    if not active then return end
    if event == "PLAYER_REGEN_DISABLED" then setCombat(true); return end
    if event == "PLAYER_REGEN_ENABLED" then setCombat(false); return end
    if event == "UI_ERROR_MESSAGE" then
        local _, message = ...
        record("error", message or "未知错误")
        local lower = message and message:lower() or ""
        local fishingError = lower:find("太浅", 1, true) or lower:find("没有鱼", 1, true)
            or lower:find("too shallow", 1, true) or lower:find("no fish", 1, true)
        if (fishingError or GetTime() - recentCast < 5) and GetTime() - recentFailure > 0.5 then
            record("cast_fail", message or "施法失败")
            recentFailure = GetTime()
        end
        return
    end
    if event == "CHAT_MSG_LOOT" then
        local message, playerName = ...
        local myName = UnitName("player")
        if playerName and playerName ~= "" and playerName ~= myName then return end
        local itemID = message and message:match("|Hitem:(%d+):")
        if itemID then
            local name = message:match("|h%[([^%]]+)%]|h") or "物品"
            local count = tonumber(message:match("x(%d+)%s*%.?$") or "1") or 1
            name = name:gsub('["\\|\r\n]', " ")
            record("loot", itemID .. "|" .. count .. "|" .. name)
        end
        return
    end
    local unit, castGUID, spellID = ...
    if unit ~= "player" or not fishingSpell(spellID) then return end
    if event == "UNIT_SPELLCAST_START" then
        recentCast = GetTime()
        lastCastGUID = castGUID
    elseif event == "UNIT_SPELLCAST_SUCCEEDED" then
        record("cast_ok", spellID)
    elseif event == "UNIT_SPELLCAST_FAILED" then
        if castGUID == lastCastGUID or GetTime() - recentFailure > 0.5 then
            record("cast_fail", spellID)
            recentFailure = GetTime()
        end
    end
end)
