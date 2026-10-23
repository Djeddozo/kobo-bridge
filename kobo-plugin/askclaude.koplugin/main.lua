--[[--
Ask Claude from KOReader. Questions go to Kobo Bridge on the Pi over Tailscale, where Claude Code answers;
documents Claude writes come back as ebooks through Syncthing.

@module koplugin.AskClaude
--]]

-- Plugin modules are required at load time, while the plugin folder is on package.path.
local AnswerViewer = require("answerviewer")
local ClaudeHome = require("homescreen")
local ConfirmBox = require("ui/widget/confirmbox")
local DataStorage = require("datastorage")
local Dispatcher = require("dispatcher")
local InfoMessage = require("ui/widget/infomessage")
local InputDialog = require("ui/widget/inputdialog")
local JSON = require("json")
local LuaSettings = require("luasettings")
local Menu = require("ui/widget/menu")
local NetworkMgr = require("ui/network/manager")
local UIManager = require("ui/uimanager")
local WidgetContainer = require("ui/widget/container/widgetcontainer")
local Screen = require("device").screen
local http = require("socket.http")
local lfs = require("libs/libkoreader-lfs")
local logger = require("logger")
local ltn12 = require("ltn12")
local socket = require("socket")
local socketutil = require("socketutil")
local socket_url = require("socket.url")
local util = require("util")
local _ = require("gettext")
local T = require("ffi/util").template

local DEFAULT_SERVER = "http://your-pi.your-tailnet.ts.net:8851"
local LIBRARY_DIR = "/mnt/onboard/Books"
local BOOKS_DIR = LIBRARY_DIR .. "/Claude"
local FREE_BOOKS_DIR = LIBRARY_DIR .. "/Free books"
-- NickelMenu's "Claude" entry creates this before starting KOReader, so we open straight on the home screen.
local HOME_FLAG = "/tmp/askclaude-home"
local POLL_SECONDS = 3
local MAX_WAIT_SECONDS = 11 * 60
local BOOK_ARRIVAL_SECONDS = 120
local DESCRIPTION_WORDS = 5 -- a "Find a book" query this long is a description, not a title or author

-- One-tap prompts. With a highlighted passage, they're about that passage.
local PASSAGE_PROMPTS = {
    { _("Explain"), _("Explain this passage simply: what's happening and why it matters.") },
    { _("Translate"), _("Translate this passage into English (or into Dutch if it's already English), keeping the tone.") },
    { _("Words"), _("Explain the difficult words and references in this passage.") },
}
local BOOK_PROMPTS = {
    { _("Recap"), _("Give me a spoiler-free recap of this book up to roughly where I'd be, plus the main characters so far.") },
    { _("Who's who"), _("List the main characters of this book with one line each, without spoilers.") },
    { _("Themes"), _("What are the main themes of this book? Keep it spoiler-free.") },
}
local BOOK_IDEA_PROMPTS = {
    { _("Like this one"), _("Suggest free classic books similar to the one I'm reading (or to classics I'd enjoy if I'm not reading one).") },
    { _("Adventure"), _("Suggest gripping free classic adventure novels.") },
    { _("Short reads"), _("Suggest short free classics I can finish in an evening.") },
    { _("Surprise me"), _("Suggest a few surprising, lesser-known free classics, each with one line on why.") },
}
local GENERAL_PROMPTS = {
    { _("News brief"), _("Make me a short news brief of today's most important world and tech news as an ebook.") },
    { _("Short story"), _("Write me an original short story (about 15 minutes of reading) as an ebook. Surprise me.") },
    { _("Learn"), _("Teach me something interesting I probably don't know, in about 5 minutes of reading.") },
}

local AskClaude = WidgetContainer:extend{
    name = "askclaude",
    is_doc_only = false,
}

function AskClaude:init()
    self.settings = LuaSettings:open(DataStorage:getSettingsDir() .. "/askclaude.lua")
    self:onDispatcherRegisterActions()
    self.ui.menu:registerToMainMenu(self)
    if self.ui.highlight then
        self.ui.highlight:addToHighlightDialog("12_ask_claude", function(this)
            return {
                text = _("Ask Claude"),
                callback = function()
                    local text = this.selected_text and util.cleanupSelectedText(this.selected_text.text)
                    this:onClose()
                    self:showAskDialog{ context = text }
                end,
            }
        end)
    end
    if lfs.attributes(HOME_FLAG, "mode") then
        os.remove(HOME_FLAG)
        UIManager:nextTick(function() self:showHome() end)
    end
end

function AskClaude:onDispatcherRegisterActions()
    Dispatcher:registerAction("ask_claude", { category = "none", event = "AskClaude", title = _("Ask Claude"), general = true })
    Dispatcher:registerAction("claude_home", { category = "none", event = "ClaudeHome", title = _("Claude home"), general = true })
end

function AskClaude:onAskClaude()
    self:showAskDialog{}
    return true
end

function AskClaude:onClaudeHome()
    self:showHome()
    return true
end

function AskClaude:addToMainMenu(menu_items)
    menu_items.ask_claude = {
        text = _("Claude"),
        sorting_hint = "tools",
        sub_item_table = {
            {
                text = _("Claude home"),
                callback = function() self:showHome() end,
            },
            {
                text = _("New question"),
                callback = function() self:showAskDialog{} end,
            },
            {
                text = _("Find a book"),
                callback = function() self:showFindBook() end,
            },
            {
                text = _("Recent answers"),
                keep_menu_open = true,
                callback = function() self:showHistory() end,
            },
            {
                text = _("Server"),
                keep_menu_open = true,
                separator = true,
                callback = function() self:editServer() end,
            },
            {
                text = _("About Ask Claude"),
                keep_menu_open = true,
                callback = function()
                    UIManager:show(InfoMessage:new{
                        text = T(_("Questions go to Claude on your Pi (%1) over Tailscale. Documents Claude writes for you arrive as ebooks in %2.\n\nTip: highlight text in a book and choose \"Ask Claude\". You can also put Ask Claude on a gesture."),
                            self:server(), BOOKS_DIR),
                    })
                end,
            },
        },
    }
end

-- Home screen ------------------------------------------------------------------------------------------

function AskClaude:showHome()
    local home
    local function go(fn)
        return function()
            UIManager:close(home, "full")
            fn()
        end
    end
    home = ClaudeHome:new{
        tiles = {
            { icon = "notice-question", title = _("Ask Claude"),
              hint = _("Questions, explanations, ebooks written for you"),
              callback = go(function() self:showAskDialog{} end) },
            { icon = "appbar.search", title = _("Find a book"),
              hint = _("Free classics from Standard Ebooks and Project Gutenberg"),
              callback = go(function() self:showFindBook() end) },
            { icon = "appbar.menu", title = _("Recent answers"),
              hint = _("Everything you asked before"),
              callback = function() self:showHistory() end },
            { icon = "star.full", title = _("Claude's ebooks"),
              hint = _("Stories, guides and briefs Claude wrote: Books › Claude"),
              callback = go(function() self:openFolder(BOOKS_DIR) end) },
            { icon = "book.opened", title = _("Free books"),
              hint = _("Books you picked in Find a book: Books › Free books"),
              callback = go(function() self:openFolder(FREE_BOOKS_DIR) end) },
        },
        footer = _("Tip: in a book, select a passage and tap \"Ask Claude\". Newer books aren't free: borrow them from your library with OverDrive in the Kobo's own menu."),
    }
    UIManager:show(home)
end

function AskClaude:openFolder(path)
    if lfs.attributes(path, "mode") ~= "directory" then
        util.makePath(path)
    end
    if self.ui.file_chooser then
        self.ui.file_chooser:changeToPath(path)
    else
        local FileManager = require("apps/filemanager/filemanager")
        self.ui:onClose()
        FileManager:showFiles(path)
    end
end

function AskClaude:openPath(path, waiting_note)
    if lfs.attributes(path, "mode") ~= "file" then
        UIManager:show(InfoMessage:new{ text = waiting_note })
        return false
    end
    if self.ui.document then
        self.ui:switchDocument(path)
    else
        local ReaderUI = require("apps/reader/readerui")
        ReaderUI:showReader(path)
    end
    return true
end

-- Settings ---------------------------------------------------------------------------------------------

function AskClaude:server()
    return self.settings:readSetting("server", DEFAULT_SERVER)
end

function AskClaude:token()
    local f = io.open(DataStorage:getSettingsDir() .. "/askclaude_token", "r")
    if not f then return nil end
    local token = f:read("*l")
    f:close()
    return token and token:match("^%s*(.-)%s*$")
end

function AskClaude:editServer()
    local dialog
    dialog = InputDialog:new{
        title = _("Kobo Bridge address"),
        input = self:server(),
        buttons = {{
            { text = _("Cancel"), id = "close", callback = function() UIManager:close(dialog) end },
            { text = _("Default"), callback = function()
                self.settings:delSetting("server"):flush()
                UIManager:close(dialog)
            end },
            { text = _("Save"), is_enter_default = true, callback = function()
                local value = dialog:getInputText():match("^%s*(.-)%s*$")
                if value ~= "" then self.settings:saveSetting("server", value):flush() end
                UIManager:close(dialog)
            end },
        }},
    }
    UIManager:show(dialog)
    dialog:onShowKeyboard()
end

-- Network ----------------------------------------------------------------------------------------------

-- KOReader's JSON decodes null as a sentinel function; turn those into plain nils.
local function strip_nulls(t)
    for k, v in pairs(t) do
        if type(v) == "function" then
            t[k] = nil
        elseif type(v) == "table" then
            strip_nulls(v)
        end
    end
    return t
end

-- Tailscale on this Kobo runs in userspace mode, so tailnet traffic goes through its local HTTP proxy.
function AskClaude:proxy()
    local log = io.open(DataStorage:getDataDir() .. "/plugins/tailscale.koplugin/bin/tailscaled.log", "r")
    if log then
        local first = log:read("*l") or ""
        log:close()
        if first:find("userspace", 1, true) then
            return "http://127.0.0.1:1056"
        end
    end
    return nil -- kernel TUN, or KOReader's own proxy setting (http.PROXY)
end

function AskClaude:request(method, path, body)
    local token = self:token()
    if not token then
        return nil, _("No access token yet. It belongs in settings/askclaude_token.")
    end
    local sink = {}
    local request = {
        url = self:server() .. path,
        method = method,
        sink = ltn12.sink.table(sink),
        headers = { ["Authorization"] = "Bearer " .. token, ["X-Kobo-Bridge"] = "1" },
        proxy = self:proxy(),
    }
    if body then
        local data = JSON.encode(body)
        request.source = ltn12.source.string(data)
        request.headers["Content-Type"] = "application/json"
        request.headers["Content-Length"] = tostring(#data)
    end
    socketutil:set_timeout(15, 30)
    local code, _headers, status = socket.skip(1, http.request(request))
    socketutil:reset_timeout()
    if type(code) ~= "number" then
        logger.warn("AskClaude: network error", code, status)
        return nil, _("Can't reach your Pi. Is Wi-Fi on and Tailscale connected?")
    end
    local ok, decoded = pcall(JSON.decode, table.concat(sink))
    if ok and type(decoded) == "table" then
        strip_nulls(decoded)
    end
    if code ~= 200 then
        local detail = ok and type(decoded) == "table" and decoded.detail
        return nil, T(_("The Pi answered %1: %2"), code, detail or status or "")
    end
    if not ok or type(decoded) ~= "table" then
        return nil, _("The Pi sent an unreadable answer.")
    end
    return decoded
end

-- Asking -----------------------------------------------------------------------------------------------

function AskClaude:bookLabel()
    local props = self.ui.document and self.ui.doc_props
    if not props or not props.display_title then return nil end
    if props.authors and props.authors ~= "" then
        return props.display_title .. " - " .. props.authors:gsub("\n", ", ")
    end
    return props.display_title
end

--- opts: context (highlighted text), conversation (id to follow up on)
function AskClaude:showAskDialog(opts)
    local book = self:bookLabel()
    local presets = opts.book_ideas and BOOK_IDEA_PROMPTS or opts.context and PASSAGE_PROMPTS
        or (book and not opts.conversation and BOOK_PROMPTS) or (not opts.conversation and GENERAL_PROMPTS) or {}
    local description
    if opts.book_ideas then
        description = _("Tell Claude what you like. It suggests free books, and \"Find these books\" gets them.")
    elseif opts.context then
        local preview = util.splitToChars(opts.context)
        description = "“" .. (#preview > 160 and table.concat(preview, "", 1, 160) .. "…" or opts.context) .. "”"
    elseif opts.conversation then
        description = _("Follow-up: Claude remembers this conversation.")
    elseif book then
        description = T(_("Reading: %1"), book)
    end

    local dialog
    local function ask(prompt)
        UIManager:close(dialog)
        self:send{ prompt = prompt, context = opts.context, book = book, conversation = opts.conversation }
    end
    local preset_row = {}
    for _i, preset in ipairs(presets) do
        table.insert(preset_row, { text = preset[1], callback = function() ask(preset[2]) end })
    end
    local buttons = {}
    if #preset_row > 0 then table.insert(buttons, preset_row) end
    table.insert(buttons, {
        { text = _("Close"), id = "close", callback = function() UIManager:close(dialog) end },
        { text = _("Recent"), callback = function()
            UIManager:close(dialog)
            self:showHistory()
        end },
        { text = _("Ask ›"), is_enter_default = true, callback = function()
            local prompt = dialog:getInputText():match("^%s*(.-)%s*$")
            if prompt == "" then
                UIManager:show(InfoMessage:new{ text = _("Type a question first, or tap one of the buttons."), timeout = 3 })
                return
            end
            ask(prompt)
        end },
    })
    dialog = InputDialog:new{
        title = opts.conversation and _("Follow-up for Claude") or _("Ask Claude"),
        description = description,
        input_hint = opts.book_ideas and _("e.g. funny classics set at sea")
            or opts.context and _("Ask about this passage…") or _("Ask anything, or ask for an ebook…"),
        allow_newline = true,
        buttons = buttons,
    }
    UIManager:show(dialog)
    dialog:onShowKeyboard()
end

function AskClaude:send(body)
    NetworkMgr:runWhenOnline(function()
        local result, err = self:request("POST", "/api/claude", body)
        if not result then
            UIManager:show(InfoMessage:new{ text = err })
            return
        end
        self:wait(result.id, body.prompt)
    end)
end

function AskClaude:wait(ask_id, prompt)
    local started = os.time()
    local cancelled = false
    local waiting = InfoMessage:new{
        text = _("Claude is thinking…\n\nThis usually takes 10 to 60 seconds; writing an ebook can take a few minutes.\n\nTap to hide. The answer stays in \"Recent answers\"."),
        dismiss_callback = function() cancelled = true end,
    }
    UIManager:show(waiting)

    local poll
    poll = function()
        if cancelled then return end
        local item, err = self:request("GET", "/api/claude/" .. ask_id)
        if item and item.status == "done" then
            UIManager:close(waiting)
            self:showAnswer(item)
        elseif item and item.status == "failed" then
            UIManager:close(waiting)
            UIManager:show(InfoMessage:new{ text = T(_("Claude couldn't answer:\n%1"), item.error or "?") })
        elseif os.time() - started > MAX_WAIT_SECONDS then
            UIManager:close(waiting)
            UIManager:show(InfoMessage:new{ text = _("Still no answer. Look in \"Recent answers\" later.") })
        else
            if err then logger.warn("AskClaude: poll failed, retrying:", err) end
            UIManager:scheduleIn(POLL_SECONDS, poll)
        end
    end
    UIManager:scheduleIn(POLL_SECONDS, poll)
end

-- Answers ----------------------------------------------------------------------------------------------

function AskClaude:openBook(name)
    return self:openPath(BOOKS_DIR .. "/" .. name,
        T(_("“%1” is still on its way. It arrives through Syncthing in a moment, in %2."), name, BOOKS_DIR))
end

function AskClaude:showAnswer(item)
    local viewer
    local row = {
        { text = _("Follow-up"), callback = function()
            UIManager:close(viewer, "full")
            self:showAskDialog{ conversation = item.id }
        end },
        { text = _("New"), callback = function()
            UIManager:close(viewer, "full")
            self:showAskDialog{}
        end },
    }
    local files = item.files or {}
    if #files == 1 then
        table.insert(row, { text = _("Open book"), callback = function()
            if self:openBook(files[1]) then UIManager:close(viewer, "full") end
        end })
    elseif #files > 1 then
        table.insert(row, { text = T(_("Books (%1)"), #files), callback = function()
            self:chooseBook(files, viewer)
        end })
    end
    table.insert(row, { text = _("Close"), callback = function() UIManager:close(viewer, "full") end })
    local rows = { row }
    local suggestions = item.suggestions or {}
    if #suggestions > 0 then
        table.insert(rows, 1, {{ text = T(_("Find these books (%1)"), #suggestions), callback = function()
            self:showSuggestions(suggestions)
        end }})
    end

    viewer = AnswerViewer:new{
        item = item,
        subtitle = item.prompt,
        buttons = rows,
    }
    UIManager:show(viewer)
end

function AskClaude:showSuggestions(suggestions)
    local items = {}
    for _i, suggestion in ipairs(suggestions) do
        table.insert(items, { text = suggestion, callback = function()
            -- "Title - Author" works as a search on both catalogs
            self:searchBooks((suggestion:gsub("%s+%-%s+", " ")), suggestion)
        end })
    end
    UIManager:show(Menu:new{ title = _("Books Claude suggested"), item_table = items, width = Screen:getWidth(),
                             height = Screen:getHeight(), covers_fullscreen = true, is_borderless = true,
                             is_popout = false })
end

-- Find a book ------------------------------------------------------------------------------------------

function AskClaude:showFindBook()
    local dialog
    local function search()
        local query = dialog:getInputText():match("^%s*(.-)%s*$")
        if query == "" then return end
        UIManager:close(dialog)
        local _spaces, words = query:gsub("%S+", "")
        if words >= DESCRIPTION_WORDS then
            -- Sounds like a wish ("something philosophical or adventurous"), not a title: that's Claude's job.
            UIManager:show(ConfirmBox:new{
                text = T(_("“%1” sounds like a description. The search only knows titles and authors.\n\nLet Claude suggest free books that fit?"), query),
                ok_text = _("Ask Claude"),
                ok_callback = function() self:askBookIdeas(query) end,
                cancel_text = _("Search anyway"),
                cancel_callback = function() self:searchBooks(query) end,
            })
            return
        end
        self:searchBooks(query)
    end
    dialog = InputDialog:new{
        title = _("Find a free book"),
        description = _("Searches Standard Ebooks and Project Gutenberg: free, legal, mostly classics."),
        input_hint = _("Title or author, e.g. Jane Austen"),
        buttons = {{
            { text = _("Close"), id = "close", callback = function() UIManager:close(dialog) end },
            { text = _("Ask Claude for ideas"), callback = function()
                UIManager:close(dialog)
                self:showAskDialog{ book_ideas = true }
            end },
            { text = _("Search ›"), is_enter_default = true, callback = search },
        }},
    }
    UIManager:show(dialog)
    dialog:onShowKeyboard()
end

function AskClaude:askBookIdeas(wish)
    self:send{
        prompt = T("Suggest free books I can get right away for this wish: %1", wish),
        book = self:bookLabel(),
    }
end

function AskClaude:searchBooks(query, label)
    NetworkMgr:runWhenOnline(function()
        local searching = InfoMessage:new{ text = T(_("Searching for “%1”…"), label or query) }
        UIManager:show(searching)
        UIManager:forceRePaint()
        local result, err = self:request("GET", "/api/books/search?q=" .. socket_url.escape(query))
        UIManager:close(searching)
        if not result then
            UIManager:show(InfoMessage:new{ text = err })
            return
        end
        local results = result.results or {}
        if #results == 0 then
            UIManager:show(ConfirmBox:new{
                text = T(_("No free book matches “%1”. The search only knows titles and authors.\n\nLet Claude suggest books for this instead? (Newer books aren't free: borrow them from your library with OverDrive in the Kobo's own menu.)"), label or query),
                ok_text = _("Ask Claude"),
                ok_callback = function() self:askBookIdeas(query) end,
            })
            return
        end
        local menu
        local items = {}
        for _i, book in ipairs(results) do
            local source = book.source == "Standard Ebooks" and _("Standard") or _("Gutenberg")
            table.insert(items, {
                text = book.author and (book.title .. " · " .. book.author) or book.title,
                mandatory = source,
                callback = function() self:confirmGetBook(book) end,
            })
        end
        menu = Menu:new{ title = T(_("Free books: %1"), label or query), item_table = items,
                         width = Screen:getWidth(), height = Screen:getHeight(), covers_fullscreen = true,
                         is_borderless = true, is_popout = false }
        UIManager:show(menu)
    end)
end

function AskClaude:confirmGetBook(book)
    UIManager:show(ConfirmBox:new{
        text = T(_("Get “%1”%2?\n\nFree from %3. It arrives in Books › Free books."),
            book.title, book.author and (" " .. _("by") .. " " .. book.author) or "", book.source),
        ok_text = _("Get it"),
        ok_callback = function() self:getBook(book) end,
    })
end

function AskClaude:getBook(book)
    NetworkMgr:runWhenOnline(function()
        local result, err = self:request("POST", "/api/books/get", { url = book.url, title = book.title })
        if not result then
            UIManager:show(InfoMessage:new{ text = err })
            return
        end
        local cancelled = false
        local waiting = InfoMessage:new{
            text = T(_("Getting “%1”…\n\nTap to hide. It still arrives in Books › Free books."), book.title),
            dismiss_callback = function() cancelled = true end,
        }
        UIManager:show(waiting)
        local started = os.time()
        local output
        local poll
        poll = function()
            if cancelled then return end
            if not output then
                local job = self:request("GET", "/api/books/job/" .. result.id)
                if job and job.status == "failed" then
                    UIManager:close(waiting)
                    UIManager:show(InfoMessage:new{ text = T(_("Couldn't get the book:\n%1"), job.error or "?") })
                    return
                end
                output = job and job.status == "done" and job.output
            end
            -- Ready on the Pi; now wait for Syncthing to bring it over.
            local path = output and (FREE_BOOKS_DIR .. "/" .. output)
            if path and lfs.attributes(path, "mode") == "file" then
                UIManager:close(waiting)
                UIManager:show(ConfirmBox:new{
                    text = T(_("“%1” is on your Kobo. Open it now?"), book.title),
                    ok_text = _("Open"),
                    ok_callback = function() self:openPath(path) end,
                })
            elseif os.time() - started > BOOK_ARRIVAL_SECONDS then
                UIManager:close(waiting)
                UIManager:show(InfoMessage:new{
                    text = T(_("“%1” is ready on your Pi and arrives in Books › Free books as soon as Syncthing catches up."), book.title),
                })
            else
                UIManager:scheduleIn(2, poll)
            end
        end
        UIManager:scheduleIn(2, poll)
    end)
end

function AskClaude:chooseBook(files, viewer)
    local menu
    local items = {}
    for _i, name in ipairs(files) do
        table.insert(items, { text = name, callback = function()
            if self:openBook(name) then
                UIManager:close(menu)
                UIManager:close(viewer, "full")
            end
        end })
    end
    menu = Menu:new{ title = _("Books from Claude"), item_table = items, width = Screen:getWidth(),
                     height = Screen:getHeight(), covers_fullscreen = true, is_borderless = true, is_popout = false }
    UIManager:show(menu)
end

function AskClaude:showHistory()
    NetworkMgr:runWhenOnline(function()
        local result, err = self:request("GET", "/api/claude")
        if not result then
            UIManager:show(InfoMessage:new{ text = err })
            return
        end
        local items = {}
        local menu
        for _i, ask in ipairs(result.asks or {}) do
            local mark = ""
            if ask.status == "failed" then
                mark = _("(failed) ")
            elseif ask.status ~= "done" then
                mark = _("(waiting) ")
            elseif type(ask.files) == "table" and #ask.files > 0 then
                mark = _("[ebook] ")
            end
            table.insert(items, {
                text = mark .. ask.prompt:gsub("%s+", " "),
                mandatory = os.date("%d %b %H:%M", math.floor(ask.created)),
                callback = function()
                    local item, e = self:request("GET", "/api/claude/" .. ask.id)
                    if not item then
                        UIManager:show(InfoMessage:new{ text = e })
                    elseif item.status == "done" then
                        self:showAnswer(item)
                    elseif item.status == "failed" then
                        UIManager:show(InfoMessage:new{ text = T(_("Claude couldn't answer:\n%1"), item.error or "?") })
                    else
                        UIManager:close(menu)
                        self:wait(item.id, item.prompt)
                    end
                end,
            })
        end
        if #items == 0 then
            UIManager:show(InfoMessage:new{ text = _("No questions yet.") })
            return
        end
        menu = Menu:new{ title = _("Recent answers from Claude"), item_table = items, width = Screen:getWidth(),
                         height = Screen:getHeight(), covers_fullscreen = true, is_borderless = true, is_popout = false }
        UIManager:show(menu)
    end)
end

return AskClaude
