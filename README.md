# Bot Google Form — Prospection

Application **Streamlit** pour remplir automatiquement un Google Form (numéro commercial + date du jour + liste de numéros clients).

## Formulaire

- URL : [Google Form](https://docs.google.com/forms/d/e/1FAIpQLSeu2db441waSJVxcePzPTBbmyHBdJUGRU7debGCJwD4rlZh7w/viewform)
- Commercial : `entry.2005620554`
- Date : `entry.1065046570` (date du jour)
- Client : `entry.1166974658`

## Fonctionnalités

- Interface mobile-friendly (Streamlit)
- Un numéro commercial pour toute la liste
- Liste clients (un par ligne)
- 1 envoi toutes les **20 secondes**
- Historique JSON local (`historique_envois.json`)
- **Anti-doublon** : numéros déjà en succès ignorés
- Bouton **Arrêter**

## Déploiement Streamlit Community Cloud

1. Fork / push ce dépôt sur GitHub
2. Aller sur [share.streamlit.io](https://share.streamlit.io)
3. New app → sélectionner le repo → fichier principal : `app.py`
4. Deploy

## Local

```bash
pip install -r requirements.txt
streamlit run app.py
```

## Note

Sur Streamlit Cloud, l’historique JSON est **éphémère** (filesystem reset). Pour un historique durable, brancher plus tard une base (Supabase) ou un fichier dans un volume.
