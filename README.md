# MG Shorts Studio

Outil interne MG Prod : transforme une vidéo longue ou un short monté en formats courts verticaux (TikTok, Reels, Shorts) habillés : recadrage sur la personne qui parle, sous-titres calés sur la voix, coupe des blancs, accroche, musique, bruitages.

Il tourne en local sur le PC (Python, ffmpeg, Whisper). Ce dépôt ne contient que le code : pas de clé API, pas de projets, pas de vidéos.

## Installer (première fois)

1. Télécharger le dépôt (Code → Download ZIP) et le dézipper.
2. Double-cliquer sur `install.bat`.
3. Lancer avec `lancer.bat` (ou le raccourci du Bureau).

## Mises à jour

Automatiques : à chaque lancement, `lancer.bat` compare le fichier `VERSION` avec celui de GitHub et récupère la nouvelle version si besoin. Les réglages (`config.json`), les projets, les musiques et les binaires ne sont jamais touchés. Pour couper la mise à jour automatique : `"auto_update": false` dans `config.json`.

Mode d'emploi détaillé : `LISEZMOI.txt`.
