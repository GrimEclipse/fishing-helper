local ADDON = ...
local frame = CreateFrame("Frame")
local active, recentCast, recentFailure, lastCastGUID = false, 0, 0, nil
local session, sequence = 0, 0
local queue, shownAt = {}, 0
local criteriaState = {}
local achievements = {2094, 2095, 1957, 2096, 10722}

-- SavedVariables are a fallback. The visible strip carries the current event live.
-- A cell stores three hex digits using colors 8, 24, ... 248 (4 px per cell).
local beacon = CreateFrame("Frame", nil, UIParent)
beacon:SetScale(1 / UIParent:GetEffectiveScale())
beacon:SetSize(18, 8)
beacon:SetPoint("TOPLEFT", UIParent, "TOPLEFT", 0, 0)
beacon:SetFrameStrata("TOOLTIP")
local combatColor = beacon:CreateTexture(nil, "OVERLAY")
combatColor:SetAllPoints()
combatColor:SetColorTexture(0, 0.8, 0.15, 1)
beacon:Hide()

local strip = CreateFrame("Frame", nil, UIParent)
strip:SetScale(1 / UIParent:GetEffectiveScale())
strip:SetSize(512, 4)
strip:SetPoint("TOPLEFT", beacon, "TOPRIGHT", 2, 0)
strip:SetFrameStrata("TOOLTIP")
local pixels = {}
for i = 1, 128 do
    local texture = strip:CreateTexture(nil, "OVERLAY")
    texture:SetSize(4, 4)
    texture:SetPoint("TOPLEFT", strip, "TOPLEFT", (i - 1) * 4, 0)
    pixels[i] = texture
end
strip:Hide()

local function fourBytes(value)
    return string.char(math.floor(value / 16777216) % 256,
        math.floor(value / 65536) % 256, math.floor(value / 256) % 256, value % 256)
end

local function showPacket(item)
    local body = item.body
    if #body > 181 then return end
    local checksum = 0
    for i = 1, #body do checksum = (checksum + body:byte(i)) % 256 end
    local packet = "FH" .. string.char(2, #body) .. fourBytes(session)
        .. string.char(math.floor(item.sequence / 256) % 256, item.sequence % 256, checksum) .. body
    for i = 1, 128 do
        local base = (i - 1) * 3
        local channels = {}
        for channel = 1, 3 do
            local nibbleIndex = base + channel - 1
            local value = packet:byte(math.floor(nibbleIndex / 2) + 1) or 0
            local nibble = nibbleIndex % 2 == 0 and math.floor(value / 16) or value % 16
            channels[channel] = (8 + nibble * 16) / 255
        end
        pixels[i]:SetColorTexture(channels[1], channels[2], channels[3], 1)
    end
    shownAt = GetTime()
end

local function enqueue(body, priority)
    if #body > 181 then return end
    local item = {body = body, sequence = sequence}
    if priority then table.insert(queue, 1, item) else queue[#queue + 1] = item end
    if #queue > 100 then table.remove(queue, 1) end
    if shownAt == 0 then showPacket(table.remove(queue, 1)) end
end

local function advanceQueue()
    if #queue > 0 and GetTime() - shownAt >= 1.5 then
        showPacket(table.remove(queue, 1))
    end
end

local function record(kind, payload)
    if not active or not FishingHelperBridgeDB then return end
    sequence = (sequence + 1) % 65536
    local clean = tostring(payload or ""):gsub('["\\\r\n]', " ")
    local stamp = time()
    local events = FishingHelperBridgeDB.events
    events[#events + 1] = "FH2|" .. session .. "|" .. sequence .. "|" .. stamp .. "|" .. kind .. "|" .. clean
    if #events > 3000 then table.remove(events, 1) end
    enqueue(stamp .. "|" .. kind .. "|" .. clean, kind == "loot" or kind == "cast_fail")
end

local function fishingSpell(spellID)
    if not spellID then return false end
    local info = C_Spell and C_Spell.GetSpellInfo and C_Spell.GetSpellInfo(spellID)
    local name = info and info.name or ""
    return name:find("钓鱼", 1, true) or name:find("抛竿", 1, true)
        or name:lower():find("fishing", 1, true)
end

local function setCombat(inCombat)
    combatColor:SetColorTexture(inCombat and 0.95 or 0, inCombat and 0.08 or 0.8, 0.15, 1)
    record(inCombat and "combat_on" or "combat_off")
end

local function achievementSnapshot(force)
    for _, achievementID in ipairs(achievements) do
        local _, name, _, earned = GetAchievementInfo(achievementID)
        if name then
            local total = GetAchievementNumCriteria(achievementID) or 0
            local completed, latest = 0, ""
            for index = 1, total do
                local criterion, _, done, _, _, _, _, _, _, criterionID = GetAchievementCriteriaInfo(achievementID, index)
                if done then completed = completed + 1 end
                local key = achievementID .. ":" .. (criterionID or index)
                if criteriaState[key] ~= nil and not criteriaState[key] and done then
                    latest = (criterion or ""):gsub("|", " ")
                end
                criteriaState[key] = done and true or false
            end
            if earned then completed = math.max(completed, total) end
            local key = "total:" .. achievementID
            if force or criteriaState[key] ~= completed then
                record("achievement", achievementID .. "|" .. completed .. "|" .. total
                    .. "|" .. name:gsub("|", " ") .. "|" .. latest)
                criteriaState[key] = completed
            end
        end
    end
end

local function toggle(value)
    active = value
    FishingHelperBridgeDB.active = value
    if value then
        session = (FishingHelperBridgeDB.session or time()) + 1
        FishingHelperBridgeDB.session = session
        sequence, queue, shownAt, criteriaState = 0, {}, 0, {}
        beacon:Show()
        strip:Show()
        frame:SetScript("OnUpdate", advanceQueue)
        frame:RegisterEvent("UNIT_SPELLCAST_START")
        frame:RegisterEvent("UNIT_SPELLCAST_SUCCEEDED")
        frame:RegisterEvent("UNIT_SPELLCAST_FAILED")
        frame:RegisterEvent("UI_ERROR_MESSAGE")
        frame:RegisterEvent("CHAT_MSG_LOOT")
        frame:RegisterEvent("PLAYER_REGEN_DISABLED")
        frame:RegisterEvent("PLAYER_REGEN_ENABLED")
        frame:RegisterEvent("CRITERIA_UPDATE")
        frame:RegisterEvent("ACHIEVEMENT_EARNED")
        record("start")
        setCombat(UnitAffectingCombat("player"))
        achievementSnapshot(true)
    else
        frame:UnregisterEvent("UNIT_SPELLCAST_START")
        frame:UnregisterEvent("UNIT_SPELLCAST_SUCCEEDED")
        frame:UnregisterEvent("UNIT_SPELLCAST_FAILED")
        frame:UnregisterEvent("UI_ERROR_MESSAGE")
        frame:UnregisterEvent("CHAT_MSG_LOOT")
        frame:UnregisterEvent("PLAYER_REGEN_DISABLED")
        frame:UnregisterEvent("PLAYER_REGEN_ENABLED")
        frame:UnregisterEvent("CRITERIA_UPDATE")
        frame:UnregisterEvent("ACHIEVEMENT_EARNED")
        beacon:Hide()
        strip:Hide()
        frame:SetScript("OnUpdate", nil)
        shownAt, queue = 0, {}
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
    if event == "CRITERIA_UPDATE" or event == "ACHIEVEMENT_EARNED" then
        achievementSnapshot(false)
        return
    end
    if event == "UI_ERROR_MESSAGE" then
        local _, message = ...
        message = (message or "未知错误"):gsub("|", " ")
        record("error", message)
        local lower = message:lower()
        local fishingError = lower:find("太浅", 1, true) or lower:find("没有鱼", 1, true)
            or lower:find("too shallow", 1, true) or lower:find("no fish", 1, true)
        if (fishingError or GetTime() - recentCast < 5) and GetTime() - recentFailure > 0.5 then
            record("cast_fail", message)
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
