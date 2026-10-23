--[[--
The Claude home screen: a full-screen page of big tiles (icon, title, one-line hint), made for e-ink and touch.
--]]

local Blitbuffer = require("ffi/blitbuffer")
local CenterContainer = require("ui/widget/container/centercontainer")
local Device = require("device")
local Font = require("ui/font")
local FrameContainer = require("ui/widget/container/framecontainer")
local Geom = require("ui/geometry")
local GestureRange = require("ui/gesturerange")
local HorizontalGroup = require("ui/widget/horizontalgroup")
local HorizontalSpan = require("ui/widget/horizontalspan")
local IconWidget = require("ui/widget/iconwidget")
local InputContainer = require("ui/widget/container/inputcontainer")
local LeftContainer = require("ui/widget/container/leftcontainer")
local Size = require("ui/size")
local TextBoxWidget = require("ui/widget/textboxwidget")
local TextWidget = require("ui/widget/textwidget")
local TitleBar = require("ui/widget/titlebar")
local UIManager = require("ui/uimanager")
local VerticalGroup = require("ui/widget/verticalgroup")
local VerticalSpan = require("ui/widget/verticalspan")
local Screen = Device.screen
local _ = require("gettext")

-- One tappable tile.
local Tile = InputContainer:extend{
    icon = nil,
    title = nil,
    hint = nil,
    width = nil,
    callback = nil,
}

function Tile:init()
    local pad = Size.padding.large
    local icon_size = Screen:scaleBySize(56)
    local text_width = self.width - icon_size - 4 * pad - 2 * Size.border.thick
    self.frame = FrameContainer:new{
        width = self.width,
        bordersize = Size.border.thick,
        radius = Size.radius.window,
        padding = pad,
        background = Blitbuffer.COLOR_WHITE,
        HorizontalGroup:new{
            align = "center",
            IconWidget:new{ icon = self.icon, width = icon_size, height = icon_size },
            HorizontalSpan:new{ width = 2 * pad },
            LeftContainer:new{
                dimen = Geom:new{ w = text_width, h = icon_size },
                VerticalGroup:new{
                    align = "left",
                    TextWidget:new{ text = self.title, face = Font:getFace("cfont", 26), bold = true,
                                    max_width = text_width },
                    VerticalSpan:new{ width = Size.span.vertical_default },
                    TextWidget:new{ text = self.hint, face = Font:getFace("cfont", 17), max_width = text_width,
                                    fgcolor = Blitbuffer.COLOR_DARK_GRAY },
                },
            },
        },
    }
    self[1] = self.frame
    self.ges_events = {
        Tap = { GestureRange:new{ ges = "tap", range = function() return self.dimen end } },
    }
end

function Tile:onTap()
    -- Brief inverted flash as feedback, like KOReader's own buttons (e-ink has no hover or ripple).
    UIManager:widgetInvert(self.frame, self.dimen.x, self.dimen.y)
    UIManager:setDirty(nil, "fast", self.dimen)
    UIManager:forceRePaint()
    UIManager:tickAfterNext(function()
        UIManager:widgetInvert(self.frame, self.dimen.x, self.dimen.y)
        UIManager:setDirty(nil, "ui", self.dimen)
        if self.callback then self.callback() end
    end)
    return true
end

local ClaudeHome = InputContainer:extend{
    tiles = nil,     -- { {icon=, title=, hint=, callback=}, ... }
    footer = nil,
    covers_fullscreen = true, -- a full page: UIManager stops painting KOReader underneath it
}

function ClaudeHome:init()
    local sw, sh = Screen:getWidth(), Screen:getHeight()
    self.dimen = Geom:new{ x = 0, y = 0, w = sw, h = sh }
    if Device:hasKeys() then
        self.key_events.Close = { { Device.input.group.Back } }
    end

    local title_bar = TitleBar:new{
        width = sw,
        fullscreen = true,
        with_bottom_line = true,
        title = _("Claude"),
        subtitle = _("Your reading companion, running on your Pi"),
        close_callback = function() self:onClose() end,
        show_parent = self,
    }

    local tile_width = sw - 2 * Screen:scaleBySize(28)
    local group = VerticalGroup:new{ align = "center" }
    for _i, t in ipairs(self.tiles) do
        table.insert(group, VerticalSpan:new{ width = Screen:scaleBySize(14) })
        table.insert(group, Tile:new{
            icon = t.icon, title = t.title, hint = t.hint, width = tile_width,
            callback = t.callback,
        })
    end
    if self.footer then
        table.insert(group, VerticalSpan:new{ width = Screen:scaleBySize(22) })
        table.insert(group, TextBoxWidget:new{
            text = self.footer,
            face = Font:getFace("cfont", 16),
            width = tile_width,
            alignment = "center",
            fgcolor = Blitbuffer.COLOR_DARK_GRAY,
        })
    end

    self[1] = FrameContainer:new{
        width = sw,
        height = sh,
        padding = 0,
        bordersize = 0,
        background = Blitbuffer.COLOR_WHITE,
        VerticalGroup:new{
            align = "center",
            title_bar,
            CenterContainer:new{
                dimen = Geom:new{ w = sw, h = group:getSize().h + Screen:scaleBySize(20) },
                group,
            },
        },
    }
end

function ClaudeHome:onShow()
    UIManager:setDirty(self, function() return "full", self.dimen end)
    return true
end

function ClaudeHome:onClose()
    -- A flashing full refresh, like the Kobo's own page changes: partial refreshes leave a ghost of this page.
    UIManager:close(self, "full")
    return true
end

return ClaudeHome
