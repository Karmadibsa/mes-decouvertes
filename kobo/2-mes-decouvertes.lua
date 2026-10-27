--[[ Mes découvertes : action « Synchroniser mes découvertes » pour KOReader.

À copier dans koreader/patches/ sur la liseuse. L'action, utilisable depuis un geste,
un profil ou le menu rapide, enchaîne :
  1. Wi-Fi allumé (sans question si « wifi_enable_action » vaut « turn_on ») ;
  2. téléchargement des nouveaux articles des catalogues OPDS marqués « synchroniser » ;
  3. Wi-Fi recoupé s'il était éteint avant ;
  4. ouverture du dossier des découvertes.
Supprimer ce fichier suffit à tout retirer. ]]

local Dispatcher = require("dispatcher")
local FileManager = require("apps/filemanager/filemanager")
local InfoMessage = require("ui/widget/infomessage")
local NetworkMgr = require("ui/network/manager")
local Trapper = require("ui/trapper")
local UIManager = require("ui/uimanager")
local logger = require("logger")

Dispatcher:registerAction("decouvertes_sync", {
    category = "none",
    event = "DecouvertesSync",
    title = "Synchroniser mes découvertes",
    filemanager = true,
})

local function synchroniser(fm, opds, OPDSBrowser)
    local browser = OPDSBrowser:new{
        servers = opds.servers,
        downloads = opds.downloads,
        settings = opds.settings,
        pending_syncs = opds.pending_syncs,
        title = "Mes découvertes",
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
    else
        UIManager:show(InfoMessage:new{ text = "Pas de nouvel article pour l'instant.", timeout = 3 })
    end
    browser.sync = false
    opds.updated = true
    opds:onFlushSettings()
    NetworkMgr:afterWifiAction()
    if fm.file_chooser then
        fm.file_chooser:changeToPath(opds.settings.sync_dir)
    end
end

function FileManager:onDecouvertesSync()
    local opds = self.opds
    local OPDSBrowser = package.loaded["opdsbrowser"]
    if not (opds and OPDSBrowser and opds.settings and opds.settings.sync_dir) then
        UIManager:show(InfoMessage:new{
            text = "Mes découvertes : le catalogue OPDS ou son dossier de synchronisation n'est pas configuré.",
        })
        return true
    end
    NetworkMgr:runWhenOnline(function()
        Trapper:wrap(function()
            local ok, err = pcall(synchroniser, self, opds, OPDSBrowser)
            if not ok then
                logger.warn("Mes découvertes : synchronisation impossible", err)
                UIManager:show(InfoMessage:new{ text = "Synchronisation impossible :\n" .. tostring(err) })
            end
        end)
    end)
    return true
end
