--[[ Mes découvertes : synchronisation des articles du jour dans KOReader.

À copier dans koreader/patches/ sur la liseuse. Supprimer ce fichier suffit à tout retirer.
  - Automatique : au premier réveil de la liseuse de la journée (après 5 h), les nouveaux
    articles sont téléchargés, puis le Wi-Fi est recoupé s'il était éteint.
  - À la main : bouton « Synchroniser mes découvertes » en tête du menu « + » de la bibliothèque,
    ou action du même nom pour un geste ou un profil.
Seuls les catalogues OPDS marqués « synchroniser » sont concernés ; les articles arrivent
dans leur dossier de synchronisation. ]]

local Dispatcher = require("dispatcher")
local FileManager = require("apps/filemanager/filemanager")
local InfoMessage = require("ui/widget/infomessage")
local NetworkMgr = require("ui/network/manager")
local ReaderUI = require("apps/reader/readerui")
local Trapper = require("ui/trapper")
local UIManager = require("ui/uimanager")
local logger = require("logger")

local AUTO_HEURE = 5           -- pas de synchronisation automatique avant 5 h
local AUTO_REESSAI = 2 * 3600  -- sans réseau, réessayer au plus toutes les 2 heures
local TEXTE_BOUTON = "Synchroniser mes découvertes"

Dispatcher:registerAction("decouvertes_sync", {
    category = "none",
    event = "DecouvertesSync",
    title = TEXTE_BOUTON,
    filemanager = true,
    reader = true,
})

local function reglages_opds(opds)
    if opds.loadSettings then -- KOReader 2026.07 et suivants
        opds:loadSettings()
        return opds.opds_settings
    end
    return opds.settings
end

local function synchroniser(ui, opds, OPDSBrowser, auto)
    local reglages = reglages_opds(opds)
    local browser = OPDSBrowser:new{
        settings = reglages,
        servers = opds.servers,
        downloads = opds.downloads,
        pending_syncs = opds.pending_syncs,
        title = TEXTE_BOUTON,
        is_popout = false,
        is_borderless = true,
        _manager = opds,
    }
    browser.sync = true
    local info = InfoMessage:new{ text = "Recherche des nouveaux articles…" }
    UIManager:show(info)
    UIManager:forceRePaint()
    for _, server in ipairs(opds.servers) do
        if server.sync then
            browser:fillPendingSyncs(server)
        end
    end
    UIManager:close(info)
    if #browser.pending_syncs > 0 then
        browser:downloadPendingSyncs()
    elseif not auto then
        UIManager:show(InfoMessage:new{ text = "Pas de nouvel article pour l'instant.", timeout = 3 })
    end
    browser.sync = false
    opds.updated = true
    opds:onFlushSettings()
    G_reader_settings:saveSetting("decouvertes_derniere_synchro", os.date("%Y-%m-%d"))
    NetworkMgr:afterWifiAction()
    if ui.file_chooser and reglages.sync_dir then
        ui.file_chooser:changeToPath(reglages.sync_dir)
    end
end

local function lancer(ui, auto)
    local opds = ui and ui.opds
    local OPDSBrowser = package.loaded["opdsbrowser"]
    local reglages = opds and OPDSBrowser and reglages_opds(opds)
    if not (reglages and reglages.sync_dir) then
        if not auto then
            UIManager:show(InfoMessage:new{
                text = "Mes découvertes : le catalogue OPDS ou son dossier de synchronisation n'est pas configuré.",
            })
        end
        return
    end
    if auto then
        if G_reader_settings:readSetting("decouvertes_derniere_synchro") == os.date("%Y-%m-%d")
                or tonumber(os.date("%H")) < AUTO_HEURE
                or os.time() - (G_reader_settings:readSetting("decouvertes_dernier_essai") or 0) < AUTO_REESSAI then
            return
        end
        G_reader_settings:saveSetting("decouvertes_dernier_essai", os.time())
    end
    NetworkMgr:runWhenOnline(function()
        Trapper:wrap(function()
            local ok, err = pcall(synchroniser, ui, opds, OPDSBrowser, auto)
            if not ok then
                logger.warn("Mes découvertes : synchronisation impossible", err)
                UIManager:show(InfoMessage:new{ text = "Synchronisation impossible :\n" .. tostring(err) })
            end
        end)
    end)
end

-- action manuelle (geste, profil)
function FileManager:onDecouvertesSync() lancer(self, false) return true end
function ReaderUI:onDecouvertesSync() lancer(self, false) return true end

-- bouton en tête du menu « + » de la bibliothèque
local boutons_plus = FileManager.getPlusDialogButtons
function FileManager:getPlusDialogButtons(...)
    local title, buttons = boutons_plus(self, ...)
    if not self.selected_files and type(buttons) == "table" then
        table.insert(buttons, 1, {{
            text = TEXTE_BOUTON,
            callback = function()
                UIManager:close(self.plus_dialog)
                lancer(self, false)
            end,
        }})
    end
    return title, buttons
end

-- automatique au réveil (une fois par jour) : on écoute l'annonce « Resume » envoyée à tous les écrans
local diffuser = UIManager.broadcastEvent
function UIManager:broadcastEvent(event, ...)
    if event and event.handler == "onResume" then
        UIManager:scheduleIn(3, function()
            lancer(FileManager.instance or ReaderUI.instance, true)
        end)
    end
    return diffuser(self, event, ...)
end
