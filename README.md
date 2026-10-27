# Mes découvertes

Chaque nuit, une petite sélection d'articles à lire sur liseuse (KOReader) : sport, physiologie, psychologie, sciences, tech, curiosités Wikipédia et un peu d'anglais simple.

- **Régler** : modifier [`reglages.yml`](reglages.yml) (thèmes, sources, nombre d'articles, durée de lecture, mots exclus).
- **Fonctionnement** : la tâche [`nuit.yml`](.github/workflows/nuit.yml) lance [`generer.py`](generer.py), qui récupère le texte complet, écarte les doublons et les contenus sponsorisés, crée un EPUB par article et un catalogue OPDS.
- **Les articles ne sont pas ici** : ils sont déposés dans un dépôt privé (lecture personnelle uniquement), que la liseuse synchronise. Seuls les 5 derniers jours y sont gardés.
- **Essai sur un PC** :

```bash
python -m pip install -r requirements.txt
python generer.py --sortie essai --url-base https://example.invalid
```

Chaque EPUB porte sa source, son auteur, sa date, son URL et sa licence dans ses métadonnées, pour que les surlignages restent traçables.
