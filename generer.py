"""Mes découvertes : prépare chaque jour une sélection d'articles pour KOReader.

Usage : python generer.py --sortie <dossier du dépôt privé> --url-base <URL brute de ce dépôt>

Le dossier de sortie contient :
  articles/*.epub        un EPUB par article (gardé `garder_jours` jours)
  opds.xml               le catalogue que la Kobo synchronise
  etat/catalogue.json    les articles encore proposés
  etat/historique.json   tout ce qui a déjà été proposé (pour éviter les doublons)
"""

import argparse
import datetime as dt
import hashlib
import html
import json
import logging
import os
import random
import re
import time
import unicodedata
import uuid
import zipfile
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

import feedparser
import requests
import trafilatura
import yaml
from lxml import etree
from lxml import html as lhtml

UA = "MesDecouvertes/1.0 (lecture personnelle sur liseuse; https://github.com/Karmadibsa/mes-decouvertes)"
MOTS_PAR_MINUTE = 230
HISTORIQUE_JOURS = 400
SECTIONS_IGNOREES = re.compile(
    r"^(notes?( et références)?|références|bibliographie|voir aussi|liens externes|articles connexes|"
    r"annexes|sources|see also|references|external links|related pages|other websites|notes)$",
    re.I,
)

log = logging.getLogger("decouvertes")
session = requests.Session()
session.headers["User-Agent"] = UA


# ---------------------------------------------------------------- outils

def get(url, **params):
    r = session.get(url, params=params or None, timeout=25)
    r.raise_for_status()
    return r


def wiki_api(langue, **params):
    params.update(format="json", formatversion=2)
    return get(f"https://{langue}.wikipedia.org/w/api.php", **params).json()


def url_canonique(url):
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc.lower(), p.path.rstrip("/"), "", ""))


def url_propre(url):
    """Retire l'ancre et les paramètres de suivi (utm_*, xtor…)."""
    p = urlsplit(url)
    requete = "&".join(x for x in p.query.split("&") if x and not re.match(r"(utm_|xtor|at_)", x))
    return urlunsplit((p.scheme, p.netloc, p.path, requete, ""))


def exclu(regl, *textes):
    texte = " ".join(textes).lower()
    return any(m.lower() in texte for m in regl.get("exclure", []))


def titre_normalise(titre):
    t = unicodedata.normalize("NFKD", titre.lower())
    return re.sub(r"[^a-z0-9]+", " ", t.encode("ascii", "ignore").decode()).strip()


def slug(texte, n=50):
    return titre_normalise(texte).replace(" ", "-")[:n].strip("-") or "article"


def nb_mots(fragment_html):
    return len(re.sub(r"<[^>]+>", " ", fragment_html).split())


# ----------------------------------------------------- nettoyage du HTML

GARDER = {"p", "h2", "h3", "h4", "ul", "ol", "li", "blockquote", "em", "strong", "b", "i",
          "sup", "sub", "br", "table", "thead", "tbody", "tr", "th", "td", "dl", "dt", "dd", "pre", "code"}
SUPPRIMER = {"script", "style", "figure", "img", "iframe", "noscript", "form", "nav", "aside",
             "footer", "header", "svg", "button", "video", "audio", "object", "picture", "source", "input"}
RENOMMER = {"h1": "h2", "h5": "h4", "h6": "h4", "head": None}
CARACTERES_INTERDITS = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f]")


def nettoyer(fragment):
    """Réduit un HTML quelconque à des blocs simples et valides en XHTML."""
    corps = re.search(r"<body[^>]*>(.*)</body>", fragment, re.S | re.I)
    if corps:
        fragment = corps.group(1)
    racine =lhtml.fragment_fromstring(CARACTERES_INTERDITS.sub("", fragment), create_parent="div")
    for el in list(racine.iter()):
        if el is racine or not isinstance(el.tag, str):
            if not isinstance(el.tag, str) and el.getparent() is not None:
                el.drop_tree()  # commentaires
            continue
        tag = el.tag.lower()
        if tag in SUPPRIMER:
            el.drop_tree()
        elif tag in RENOMMER and RENOMMER[tag]:
            el.tag = RENOMMER[tag]
            el.attrib.clear()
        elif tag in GARDER:
            el.attrib.clear()
        else:
            el.drop_tag()
    # paragraphes vides
    for p in racine.findall(".//p"):
        if not (p.text_content() or "").strip() and p.getparent() is not None:
            p.drop_tree()
    blocs = []
    if racine.text and racine.text.strip():
        blocs.append(f"<p>{html.escape(racine.text.strip())}</p>")
    for enfant in racine:
        xml = etree.tostring(enfant, method="xml", encoding="unicode", with_tail=False)
        blocs.append(xml)
        if enfant.tail and enfant.tail.strip():
            blocs.append(f"<p>{html.escape(enfant.tail.strip())}</p>")
    return "\n".join(blocs)


def couper(blocs_html, mots_cible, mots_max):
    """Garde les blocs jusqu'à `mots_cible` (coupe de préférence à un titre de section)."""
    racine = lhtml.fragment_fromstring(blocs_html, create_parent="div")
    garde, total, coupe = [], 0, False
    for el in racine:
        if el.tag == "h2" and SECTIONS_IGNOREES.match(el.text_content().strip()):
            coupe = True
            break
        if el.tag in ("h2", "h3") and total >= mots_cible:
            coupe = True
            break
        n = len(el.text_content().split())
        if total + n > mots_max and total >= mots_cible // 2:
            coupe = True
            break
        garde.append(etree.tostring(el, method="xml", encoding="unicode", with_tail=False))
        total += n
    # pas de titre orphelin à la fin
    while garde and re.match(r"<h[234]", garde[-1]):
        garde.pop()
    return "\n".join(garde), coupe


# ------------------------------------------------------------- sources

class Article(dict):
    """titre, url, cle, source, auteur, date, licence, langue, theme, corps (HTML), chapeau (facultatif)"""


def candidats_rss(src, theme, regl):
    flux = feedparser.parse(get(src["url"]).content)
    mots_cles = [m.lower() for m in src.get("mots_cles", [])]
    sortie = []
    for e in flux.entries:
        url = url_propre(e.get("link", ""))
        titre = html.unescape(e.get("title", "")).strip()
        if not url or not titre or exclu(regl, titre, url):
            continue
        contenu = ""
        if e.get("content"):
            contenu = max((c.get("value", "") for c in e.content), key=len)
        resume = e.get("summary", "")
        if mots_cles:
            # titre + début du texte seulement, et mots entiers (« sport » ne doit pas trouver « transport »)
            debut = re.sub(r"<[^>]+>", " ", resume or contenu)[:800]
            texte = f"{titre} {debut}".lower()
            if not any(re.search(r"\b" + re.escape(m), texte) for m in mots_cles):
                continue
        date = None
        for champ in ("published_parsed", "updated_parsed"):
            if e.get(champ):
                date = dt.date(*e[champ][:3]).isoformat()
                break
        auteur = e.get("author", "")
        sortie.append(Article(
            titre=titre, url=url, cle=url_canonique(url), source=src.get("nom") or flux.feed.get("title", "Web"),
            auteur=auteur, date=date, licence=src.get("licence", ""), langue=src.get("langue", "fr"),
            theme=theme, _contenu_flux=contenu, _type="rss",
        ))
    return sortie


def finaliser_rss(a, regl):
    """Récupère le texte complet : page de l'article, sinon contenu du flux."""
    corps = ""
    try:
        page = get(a["url"]).content  # octets : trafilatura détecte lui-même l'encodage
        corps = trafilatura.extract(page, output_format="html", include_images=False, include_links=False,
                                    include_tables=True, include_comments=False, favor_precision=True) or ""
        if not a.get("auteur"):
            meta = trafilatura.extract_metadata(page)
            if meta and meta.author:
                a["auteur"] = meta.author
    except Exception as err:  # noqa: BLE001
        log.info("  page illisible (%s) : %s", err, a["url"])
    if "@" in a.get("auteur", "") or a.get("auteur", "").lower() == a["source"].lower():
        a["auteur"] = ""
    corps = nettoyer(corps) if corps else ""
    flux = nettoyer(a["_contenu_flux"]) if a.get("_contenu_flux") else ""
    if nb_mots(flux) > nb_mots(corps):
        corps = flux
    # le titre est déjà affiché : retirer un éventuel premier titre identique
    corps = re.sub(r"^<h2>[^<]*</h2>\s*", "", corps) if corps.startswith("<h2>") else corps
    if exclu(regl, a["titre"], a["url"], a.get("auteur", ""), corps[:1500], corps[-1500:]):
        log.info("  exclu : %s", a["titre"])
        return None
    mots = nb_mots(corps)
    if not (regl["mots_min"] <= mots <= regl["mots_max_rss"]):
        log.info("  écarté (%d mots) : %s", mots, a["titre"])
        return None
    a["corps"] = corps
    return a


def membres_categorie(langue, categorie):
    prefixe = "Catégorie" if langue == "fr" else "Category"
    data = wiki_api(langue, action="query", list="categorymembers", cmtitle=f"{prefixe}:{categorie}",
                    cmtype="page", cmlimit=500)
    return [m["title"] for m in data.get("query", {}).get("categorymembers", [])]


def candidats_wikipedia(src, theme, regl):
    langue = src.get("langue", "fr")
    titres = []
    for cat in src.get("categories", []):
        try:
            membres = membres_categorie(langue, cat)
        except Exception as err:  # noqa: BLE001
            log.warning("  catégorie %s illisible : %s", cat, err)
            continue
        if not membres:
            log.warning("  catégorie vide ou absente : %s (%s)", cat, langue)
        titres += membres
    random.shuffle(titres)
    return [article_wikipedia_vide(langue, t, theme) for t in titres[:60]]


def candidats_wikipedia_qualite(src, theme, regl):
    langue = src.get("langue", "fr")
    cat = random.choice(["Article_de_qualité", "Bon_article"]) if langue == "fr" else "Good_articles"
    data = wiki_api(langue, action="query", list="search", srsearch=f'incategory:"{cat}"',
                    srsort="random", srlimit=30, srnamespace=0)
    return [article_wikipedia_vide(langue, r["title"], theme) for r in data.get("query", {}).get("search", [])]


def candidats_wikipedia_anecdote(src, theme, regl):
    annee = dt.date.today().year
    pages = [f"Wikipédia:Le saviez-vous ?/Archives/{a}" for a in (annee, annee - 1, annee - 2)]
    sortie = []
    for page in pages:
        try:
            texte = wiki_api("fr", action="parse", page=page, prop="text")["parse"]["text"]
        except Exception:  # noqa: BLE001
            continue
        for li in re.findall(r"<li>(.*?)</li>", texte, re.S):
            cible = re.search(r'<b>\s*<a href="/wiki/([^"#]+)"', li)
            if not cible:
                continue
            titre = requests.utils.unquote(cible.group(1)).replace("_", " ")
            phrase = html.unescape(re.sub(r"<[^>]+>", "", li)).strip()
            if len(phrase) < 30:
                continue
            a = article_wikipedia_vide("fr", titre, theme)
            a["chapeau"] = f"Le saviez-vous ? {phrase}"
            a["mots_min"] = 150  # l'anecdote suffit à donner envie, même si l'article est court
            sortie.append(a)
    random.shuffle(sortie)
    return sortie[:60]


def article_wikipedia_vide(langue, titre, theme):
    domaine = f"{langue}.wikipedia.org"
    url = f"https://{domaine}/wiki/{quote(titre.replace(' ', '_'))}"
    return Article(
        titre=titre, url=url, cle=f"wp:{langue}:{titre}", source="Simple English Wikipedia" if langue == "simple" else "Wikipédia",
        auteur="Contributeurs de Wikipédia", date=None, licence="CC BY-SA 4.0",
        langue="en" if langue == "simple" else langue, theme=theme, _type="wikipedia", _wiki=langue,
    )


def finaliser_wikipedia(a, regl):
    data = wiki_api(a["_wiki"], action="query", prop="extracts|info", titles=a["titre"], redirects=1)
    page = data["query"]["pages"][0]
    if page.get("missing") or "extract" not in page:
        return None
    if a["titre"] != page["title"]:
        a["titre"] = page["title"]
        a["url"] = f"https://{a['_wiki']}.wikipedia.org/wiki/{quote(page['title'].replace(' ', '_'))}"
    corps = nettoyer(page["extract"])
    corps, coupe = couper(corps, regl["mots_cible_wiki"], regl["mots_max"])
    if coupe:
        lire = "Read the rest on Simple English Wikipedia" if a["_wiki"] == "simple" else "La suite de l'article est sur Wikipédia"
        corps += f'\n<p class="suite">{lire} : {html.escape(a["url"])}</p>'
    mots = nb_mots(corps)
    if mots < a.get("mots_min", regl["mots_min"]):
        log.info("  écarté (%d mots) : %s", mots, a["titre"])
        return None
    a["date"] = (page.get("touched") or "")[:10] or None
    a["corps"] = corps
    return a


COLLECTEURS = {
    "rss": (candidats_rss, finaliser_rss),
    "wikipedia": (candidats_wikipedia, finaliser_wikipedia),
    "wikipedia_qualite": (candidats_wikipedia_qualite, finaliser_wikipedia),
    "wikipedia_anecdote": (candidats_wikipedia_anecdote, finaliser_wikipedia),
}


# ------------------------------------------------------------ sélection

def quotas(regl):
    themes = regl["themes"]
    total_parts = sum(t.get("part", 0) for t in themes) or 1
    q = {}
    for t in themes:
        q[t["nom"]] = t["nombre"] if "nombre" in t else round(regl["articles_par_jour"] * t.get("part", 0) / total_parts)
    return q


def selectionner(regl, deja_vus):
    choisis, titres_vus = [], set()
    cache_flux = {}
    for theme, quota in quotas(regl).items():
        if quota <= 0:
            continue
        conf = next(t for t in regl["themes"] if t["nom"] == theme)
        log.info("Thème %s : %d article(s)", theme, quota)
        # un tas de candidats par source, puis tirage en alternant les sources
        tas = []
        for src in conf["sources"]:
            collecte, _ = COLLECTEURS[src["type"]]
            cle_cache = (src["type"], src.get("url"), json.dumps(src.get("mots_cles", [])), json.dumps(src.get("categories", [])))
            try:
                if cle_cache not in cache_flux:
                    cache_flux[cle_cache] = collecte(src, theme, regl)
                candidats = [Article(c, theme=theme) for c in cache_flux[cle_cache]]
            except Exception as err:  # noqa: BLE001
                log.warning("  source en échec (%s) : %s", src.get("url") or src["type"], err)
                continue
            candidats = [c for c in candidats if c["cle"] not in deja_vus and titre_normalise(c["titre"]) not in deja_vus]
            if src["type"] == "rss":
                candidats.sort(key=lambda c: c.get("date") or "", reverse=True)
                candidats = candidats[:15]
                random.shuffle(candidats)
            log.info("  %s : %d candidat(s)", src.get("nom") or src["type"], len(candidats))
            tas.append((src["type"], candidats))
        random.shuffle(tas)
        pris, essais = 0, 0
        while pris < quota and any(c for _, c in tas) and essais < quota * 8:
            for type_src, candidats in tas:
                if pris >= quota or not candidats:
                    continue
                a = candidats.pop(0)
                essais += 1
                tn = titre_normalise(a["titre"])
                if a["cle"] in titres_vus or tn in titres_vus:
                    continue
                try:
                    a = COLLECTEURS[type_src][1](a, regl)
                    time.sleep(0.3)
                except Exception as err:  # noqa: BLE001
                    log.info("  échec %s : %s", a["url"], err)
                    a = None
                if a:
                    titres_vus.update({a["cle"], tn})
                    choisis.append(a)
                    pris += 1
                    log.info("  + %s (%s, %d mots)", a["titre"], a["source"], nb_mots(a["corps"]))
        if pris < quota:
            log.warning("  thème %s incomplet : %d/%d", theme, pris, quota)
    return choisis


# ------------------------------------------------------------------ EPUB

CSS = """body { margin: 0 0.4em; line-height: 1.45; }
h1 { font-size: 1.35em; margin: 0.2em 0 0.3em; line-height: 1.2; }
h2 { font-size: 1.15em; margin-top: 1.2em; }
h3, h4 { font-size: 1em; margin-top: 1em; }
.meta { font-size: 0.85em; font-style: italic; margin-bottom: 1em; }
.chapeau { font-weight: bold; margin: 0.8em 0 1.2em; }
.suite, .source { font-size: 0.85em; margin-top: 1.5em; }
blockquote { margin: 0.8em 1em; font-style: italic; }
"""


def description(a):
    morceaux = [f"Source : {a['source']}"]
    if a.get("auteur"):
        morceaux.append(f"Auteur : {a['auteur']}")
    if a.get("date"):
        morceaux.append(f"Publié le : {a['date']}")
    morceaux.append(f"URL : {a['url']}")
    morceaux.append(f"Récupéré le : {a['recupere']}")
    if a.get("licence"):
        morceaux.append(f"Licence : {a['licence']}")
    morceaux.append(f"Thème : {a['theme']}")
    return " — ".join(morceaux)


def ecrire_epub(a, chemin):
    e = html.escape
    ident = str(uuid.uuid5(uuid.NAMESPACE_URL, a["cle"]))
    langue = a["langue"]
    minutes = max(1, round(nb_mots(a["corps"]) / MOTS_PAR_MINUTE))
    meta = " · ".join(x for x in [a["source"], a.get("auteur"), a.get("date"), f"{minutes} min", a["theme"]] if x)
    chapeau = f'<p class="chapeau">{e(a["chapeau"])}</p>' if a.get("chapeau") else ""
    source = f'Source : {e(a["source"])} — {e(a["url"])}' + (f' — {e(a["licence"])}' if a.get("licence") else "")
    xhtml = f"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="{langue}" lang="{langue}">
<head><meta charset="utf-8"/><title>{e(a["titre"])}</title><link rel="stylesheet" type="text/css" href="style.css"/></head>
<body>
<h1>{e(a["titre"])}</h1>
<p class="meta">{e(meta)}</p>
{chapeau}
{a["corps"]}
<p class="source">{source}</p>
</body>
</html>
"""
    etree.fromstring(xhtml.encode("utf-8"))  # lève une erreur si le XHTML est invalide
    nav = f"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
<head><meta charset="utf-8"/><title>{e(a["titre"])}</title></head>
<body><nav epub:type="toc"><ol><li><a href="article.xhtml">{e(a["titre"])}</a></li></ol></nav></body>
</html>
"""
    modifie = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    opf = f"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid" xml:lang="{langue}">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
<dc:identifier id="uid">urn:uuid:{ident}</dc:identifier>
<dc:title>{e(a["titre"])}</dc:title>
<dc:creator>{e(a["source"])}</dc:creator>
<dc:publisher>{e(a["source"])}</dc:publisher>
<dc:language>{langue}</dc:language>
<dc:date>{e(a.get("date") or a["recupere"])}</dc:date>
<dc:source>{e(a["url"])}</dc:source>
<dc:subject>{e(a["theme"])}</dc:subject>
<dc:description>{e(description(a))}</dc:description>
{f'<dc:rights>{e(a["licence"])}</dc:rights>' if a.get("licence") else ""}
<meta property="dcterms:modified">{modifie}</meta>
</metadata>
<manifest>
<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
<item id="article" href="article.xhtml" media-type="application/xhtml+xml"/>
<item id="css" href="style.css" media-type="text/css"/>
</manifest>
<spine><itemref idref="article"/></spine>
</package>
"""
    container = """<?xml version="1.0" encoding="utf-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>
"""
    with zipfile.ZipFile(chemin, "w") as z:
        z.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", container, compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("OEBPS/content.opf", opf, compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("OEBPS/nav.xhtml", nav, compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("OEBPS/article.xhtml", xhtml, compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("OEBPS/style.css", CSS, compress_type=zipfile.ZIP_DEFLATED)
    return minutes


# ------------------------------------------------------------------ OPDS

def ecrire_opds(catalogue, url_base, chemin):
    e = html.escape
    maintenant = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    entrees = []
    for c in catalogue:
        href = url_base.rstrip("/") + "/articles/" + quote(c["fichier"])
        entrees.append(f"""<entry>
<title>{e(c["titre"])}</title>
<id>urn:uuid:{c["id"]}</id>
<updated>{c["jour"]}T05:00:00Z</updated>
<author><name>{e(c["source"])}</name></author>
<dc:language>{c["langue"]}</dc:language>
<summary>{e(c["theme"])} · {c["minutes"]} min — {e(c["source"])}</summary>
<link rel="http://opds-spec.org/acquisition" href="{e(href)}" type="application/epub+zip"/>
</entry>""")
    chemin.write_text(f"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:dc="http://purl.org/dc/terms/" xmlns:opds="http://opds-spec.org/2010/catalog">
<id>urn:mes-decouvertes</id>
<title>Mes découvertes</title>
<updated>{maintenant}</updated>
<author><name>Mes découvertes</name></author>
{chr(10).join(entrees)}
</feed>
""", encoding="utf-8")


# ---------------------------------------------------------------- principal

def charger_json(chemin, defaut):
    try:
        return json.loads(chemin.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return defaut


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sortie", required=True, type=Path)
    ap.add_argument("--url-base", required=True, help="URL publique ou brute du dossier de sortie")
    ap.add_argument("--reglages", default=Path(__file__).with_name("reglages.yml"), type=Path)
    ap.add_argument("--jour", default=dt.date.today().isoformat())
    args = ap.parse_args()
    # dans GitHub Actions (journal public), DECOUVERTES_LOG=WARNING évite d'afficher les titres choisis
    logging.basicConfig(level=os.environ.get("DECOUVERTES_LOG", "INFO"), format="%(message)s")

    regl = yaml.safe_load(args.reglages.read_text(encoding="utf-8"))
    regl["mots_min"] = regl["duree_min"] * MOTS_PAR_MINUTE
    regl["mots_max"] = regl["duree_max"] * MOTS_PAR_MINUTE
    regl["mots_max_rss"] = regl["mots_max"]
    regl["mots_cible_wiki"] = (regl["duree_min"] + regl["duree_max"]) // 2 * MOTS_PAR_MINUTE

    sortie = args.sortie
    (sortie / "articles").mkdir(parents=True, exist_ok=True)
    (sortie / "etat").mkdir(exist_ok=True)
    historique = charger_json(sortie / "etat" / "historique.json", {})
    catalogue = charger_json(sortie / "etat" / "catalogue.json", [])
    jour = args.jour

    if any(c["jour"] == jour for c in catalogue):
        log.info("Déjà généré pour %s : rien à faire.", jour)
    else:
        deja_vus = set(historique)
        nouveaux = selectionner(regl, deja_vus)
        random.shuffle(nouveaux)
        for ordre, a in enumerate(nouveaux):
            a["recupere"] = jour
            fichier = f"{jour}-{ordre:02d}-{slug(a['titre'])}-{hashlib.sha1(a['cle'].encode()).hexdigest()[:6]}.epub"
            try:
                minutes = ecrire_epub(a, sortie / "articles" / fichier)
            except Exception as err:  # noqa: BLE001
                log.warning("EPUB impossible pour %s : %s", a["titre"], err)
                continue
            catalogue.append(dict(fichier=fichier, titre=a["titre"], source=a["source"], theme=a["theme"],
                                  langue=a["langue"], minutes=minutes, jour=jour, ordre=ordre,
                                  id=str(uuid.uuid5(uuid.NAMESPACE_URL, a["cle"]))))
            historique[a["cle"]] = jour
            historique[titre_normalise(a["titre"])] = jour
        log.info("%d article(s) ajouté(s) pour %s.", len(nouveaux), jour)

    # rotation : ne proposer que les `garder_jours` derniers jours
    limite = (dt.date.fromisoformat(jour) - dt.timedelta(days=regl["garder_jours"] - 1)).isoformat()
    catalogue = [c for c in catalogue if c["jour"] >= limite]
    gardes = {c["fichier"] for c in catalogue}
    for f in (sortie / "articles").glob("*.epub"):
        if f.name not in gardes:
            f.unlink()
    limite_hist = (dt.date.fromisoformat(jour) - dt.timedelta(days=HISTORIQUE_JOURS)).isoformat()
    historique = {k: v for k, v in historique.items() if v >= limite_hist}

    # le plus récent en premier : c'est ce qu'attend la synchronisation de KOReader
    catalogue.sort(key=lambda c: (c["jour"], -c["ordre"]), reverse=True)
    ecrire_opds(catalogue, args.url_base, sortie / "opds.xml")
    (sortie / "etat" / "catalogue.json").write_text(json.dumps(catalogue, ensure_ascii=False, indent=1), encoding="utf-8")
    (sortie / "etat" / "historique.json").write_text(json.dumps(historique, ensure_ascii=False, indent=0), encoding="utf-8")


if __name__ == "__main__":
    main()
