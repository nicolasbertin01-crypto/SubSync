# SubSync

Logiciel Windows (Python + customtkinter) pour :

- **resynchroniser des sous-titres .srt** sur l'audio d'une vidéo (modes Automatique / Simple / Avancé) ;
- **créer des sous-titres français** à partir de l'audio (Whisper + traduction par IA locale) ;
- **fusionner une VF** d'une version de moins bonne qualité dans une vidéo HD (calage automatique, sous-titres VF recalés) ;
- **améliorer l'image** de vieilles vidéos (agrandissement Real-ESRGAN + restauration des visages, sur la carte graphique via DirectML).

## Installation

1. Télécharger le zip de la dernière version (onglet *Releases*) et le décompresser.
2. Double-cliquer **Installer SubSync.bat**.
3. Optionnel : **Installer l'IA de traduction.bat** et **Installer l'amélioration vidéo.bat**.

Le mode d'emploi détaillé est dans `LISEZ-MOI.txt`.

## Fichiers locaux non versionnés

Environnements Python (`.venv`, `.venv_video`, `python/`), caches, journaux, modèles téléchargés
(Whisper, traduction, IA locale, visages) et réglages personnels (`reglages.json`).
`glossaire.txt` n'est jamais écrasé par une mise à jour s'il existe déjà.
