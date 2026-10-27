#!/usr/bin/env bash
# À lancer une seule fois : autorise la tâche nocturne à écrire dans le dépôt privé des articles.
# Crée une clé dédiée, l'ajoute comme clé de déploiement (écriture) au dépôt privé
# et la range dans les secrets du dépôt public. La clé locale est ensuite effacée.
set -euo pipefail
dossier=$(mktemp -d)
ssh-keygen -q -t ed25519 -N "" -C "mes-decouvertes" -f "$dossier/cle"
gh repo deploy-key add "$dossier/cle.pub" -R Karmadibsa/mes-decouvertes-articles --allow-write --title "Tâche nocturne mes-decouvertes"
gh secret set CLE_DEPOT_ARTICLES -R Karmadibsa/mes-decouvertes < "$dossier/cle"
rm -rf "$dossier"
echo "C'est fait. Tu peux lancer la tâche : gh workflow run nuit.yml -R Karmadibsa/mes-decouvertes"
