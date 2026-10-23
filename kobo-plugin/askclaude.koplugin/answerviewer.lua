--[[--
Full-screen page for one Claude answer: title bar, the reply rendered as HTML, and action buttons.
--]]

local Blitbuffer = require("ffi/blitbuffer")
local ButtonTable = require("ui/widget/buttontable")
local CenterContainer = require("ui/widget/container/centercontainer")
local Device = require("device")
local FrameContainer = require("ui/widget/container/framecontainer")
local Geom = require("ui/geometry")
local InputContainer = require("ui/widget/container/inputcontainer")
local ScrollHtmlWidget = require("ui/widget/scrollhtmlwidget")
local Size = require("ui/size")
local TitleBar = require("ui/widget/titlebar")
local UIManager = require("ui/uimanager")
local VerticalGroup = require("ui/widget/verticalgroup")
local VerticalSpan = require("ui/widget/verticalspan")
local Screen = Device.screen
local _ = require("gettext")

local AnswerViewer = InputContainer:extend{
    item = nil,      -- the ask, as returned by Kobo Bridge
    buttons = nil,   -- rows of ButtonTable buttons
    subtitle = nil,
    covers_fullscreen = true, -- a full page: UIManager stops painting KOReader underneath it
}

function AnswerViewer:init()
    local sw, sh = Screen:getWidth(), Screen:getHeight()
    self.dimen = Geom:new{ x = 0, y = 0, w = sw, h = sh }
    local pad = Size.padding.large

    if Device:hasKeys() then
        self.key_events.Close = { { Device.input.group.Back } }
    end

    local title_bar = TitleBar:new{
        width = sw,
        fullscreen = true,
        with_bottom_line = true,
        title = _("Claude"),
        subtitle = self.subtitle,
        close_callback = function() self:onClose() end,
        show_parent = self,
    }
    local button_table = ButtonTable:new{
        width = sw - 2 * pad,
        buttons = self.buttons,
        zero_sep = true,
        show_parent = self,
    }
    local html_height = sh - title_bar:getHeight() - button_table:getSize().h - 3 * pad
    self.html_widget = ScrollHtmlWidget:new{
        html_body = self.item.html or "",
        css = self.item.css,
        default_font_size = Screen:scaleBySize(21),
        width = sw - 2 * pad,
        height = html_height,
        dialog = self,
    }

    self[1] = FrameContainer:new{
        width = sw,
        height = sh,
        padding = 0,
        bordersize = 0,
        background = Blitbuffer.COLOR_WHITE,
        VerticalGroup:new{
            align = "center",
            title_bar,
            VerticalSpan:new{ width = pad },
            CenterContainer:new{
                dimen = Geom:new{ w = sw, h = html_height },
                self.html_widget,
            },
            VerticalSpan:new{ width = pad },
            button_table,
        },
    }
end

function AnswerViewer:onShow()
    UIManager:setDirty(self, function() return "full", self.dimen end)
    return true
end

function AnswerViewer:onClose()
    -- A flashing full refresh, like the Kobo's own page changes: partial refreshes leave a ghost of this page.
    UIManager:close(self, "full")
    return true
end

return AnswerViewer
