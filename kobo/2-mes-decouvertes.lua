--[[ Mes découvertes : synchronisation des articles du jour dans KOReader.

À copier dans koreader/patches/ sur la liseuse. Supprimer ce fichier suffit à tout retirer.
  - Automatique : au premier réveil de la liseuse de la journée (après 5 h), les nouveaux
    articles sont téléchargés, puis le Wi-Fi est recoupé s'il était éteint.
  - À la main : bouton « Synchroniser mes découvertes » en tête du menu « + » de la bibliothèque,
    ou action du même nom pour un geste ou un profil.
Seuls les catalogues OPDS marqués « synchroniser » sont concernés ; les articles arrivent
dans leur dossier de synchronisation.
Avant chaque synchronisation, ce dossier est nettoyé : les articles lus (après la journée de
lecture) et les articles non lus depuis plus de 5 jours sont supprimés. Sont toujours gardés :
un article surligné, annoté, avec marque-page, dans les favoris ou une collection, ouvert à l'écran. ]]

local DocSettings = require("docsettings")
local Dispatcher = require("dispatcher")
local FileManager = require("apps/filemanager/filemanager")
local InfoMessage = require("ui/widget/infomessage")
local NetworkMgr = require("ui/network/manager")
local ReadCollection = require("readcollection")
local ReaderUI = require("apps/reader/readerui")
local Trapper = require("ui/trapper")
local UIManager = require("ui/uimanager")
local lfs = require("libs/libkoreader-lfs")
local logger = require("logger")

local AUTO_HEURE = 5           -- pas de synchronisation automatique avant 5 h
local AUTO_REESSAI = 2 * 3600  -- sans réseau, réessayer au plus toutes les 2 heures
local GARDER_JOURS = 5         -- un article non lu est supprimé après ce délai
local LU = 0.9                 -- part lue à partir de laquelle un article compte comme lu
local TEXTE_BOUTON = "Synchroniser mes découvertes"

local function a_garder(fichier, ouvert)
    if fichier == ouvert or ReadCollection:isFileInCollections(fichier, true) then
        return true
    end
    local sidecar = DocSettings:findSidecarFile(fichier)
    if sidecar then
        local ds = DocSettings:open(fichier)
        local annotations = ds:readSetting("annotations")
        if (type(annotations) == "table" and #annotations > 0)
                or next(ds:readSetting("highlight") or {}) or next(ds:readSetting("bookmarks") or {}) then
            return true -- surlignages, notes, marque-pages : pour Jalynopédie
        end
        -- ouvert dans les dernières 24 h : on laisse finir la journée de lecture
        if os.time() - (lfs.attributes(sidecar, "modification") or 0) < 24 * 3600 then
            return true
        end
        local resume = ds:readSetting("summary") or {}
        if resume.status == "complete" or (ds:readSetting("percent_finished") or 0) >= LU then
            return false -- lu
        end
    end
    return os.time() - (lfs.attributes(fichier, "modification") or os.time()) < GARDER_JOURS * 24 * 3600
end

local function nettoyer(dossier)
    if lfs.attributes(dossier, "mode") ~= "directory" then return 0 end
    local doc = ReaderUI.instance and ReaderUI.instance.document
    local ouvert = doc and doc.file
    local a_supprimer = {}
    for nom in lfs.dir(dossier) do
        local fichier = dossier .. "/" .. nom
        if nom:lower():match("%.epub$") and lfs.attributes(fichier, "mode") == "file"
                and not a_garder(fichier, ouvert) then
            table.insert(a_supprimer, fichier)
        end
    end
    local n = 0
    for _, fichier in ipairs(a_supprimer) do
        if FileManager:deleteFile(fichier, true) then n = n + 1 end
    end
    logger.info("Mes découvertes : articles supprimés", n)
    return n
end

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
    nettoyer(reglages.sync_dir)
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
