# SMS Chat

Application de messagerie en temps réel : comptes utilisateurs, messages privés instantanés,
indicateur de frappe, accusés de lecture, réponses/édition/suppression de messages, emojis,
stories 24h avec suivi des vues, profils, et mode clair/sombre.

Testée de bout en bout en local (PostgreSQL réel) : inscription, connexion, recherche,
envoi/édition/suppression de messages, permissions, stories et vues — tout fonctionne.

## Technologies

- **Backend** : Python, Flask, Flask-SocketIO (mode `threading`, pas de dépendance à eventlet)
- **Base de données** : PostgreSQL (via `psycopg`)
- **Frontend** : HTML/Jinja2, CSS, JavaScript (Socket.IO client via CDN)
- **Sécurité** : mots de passe hashés (Werkzeug), sessions Flask, requêtes SQL paramétrées

## Variables d'environnement requises

| Variable       | Rôle                                              |
|----------------|----------------------------------------------------|
| `DATABASE_URL` | URL de connexion PostgreSQL                         |
| `SECRET_KEY`   | Clé secrète Flask (sessions) — génère une chaîne aléatoire longue |

Sans ces deux variables, l'application démarre mais affiche des erreurs claires
au lieu de planter silencieusement.

---

## Déployer sur Render (étape par étape)

### 1. Mets le code sur un dépôt Git
Render déploie depuis un dépôt (GitHub, GitLab, ou Bitbucket). Crée un dépôt et pousse
le contenu de ce dossier dedans — c'est la seule façon dont Render peut récupérer le code
(il ne prend pas de zip en upload direct pour un Web Service).

### 2. Crée la base de données PostgreSQL
1. Sur [render.com](https://render.com), clique **New +** → **PostgreSQL**.
2. Donne-lui un nom (ex. `smschat-db`), choisis la région, plan **Free** ou **Starter**.
3. Clique **Create Database**.
4. Une fois créée, ouvre sa page et copie la valeur **Internal Database URL**
   (commence par `postgresql://...`).

### 3. Crée le service web
1. Clique **New +** → **Web Service**.
2. Connecte le dépôt Git que tu as créé à l'étape 1.
3. Renseigne :
   - **Name** : `sms-chat` (ou ce que tu veux)
   - **Runtime** : Python 3
   - **Build Command** : `pip install -r requirements.txt`
   - **Start Command** : `python app.py`
4. Dans **Environment Variables**, ajoute :
   - `DATABASE_URL` → colle l'Internal Database URL copiée à l'étape 2
   - `SECRET_KEY` → une chaîne aléatoire longue (tu peux en générer une avec
     `python3 -c "import secrets; print(secrets.token_hex(32))"` sur ton ordinateur)
5. Clique **Create Web Service**.

### 4. Attends le déploiement
Render installe les dépendances et lance l'application. Les tables PostgreSQL sont créées
automatiquement au démarrage (voir `init_db()` dans `app.py`). Suis les logs dans l'onglet
**Logs** — tu dois voir `Base de données initialisée (tables + index).`

### 5. Teste
Une fois le statut **Live**, ouvre l'URL fournie par Render (`https://sms-chat-xxxx.onrender.com`) :
- `/health` doit répondre `{"status": "ok"}`
- Crée un compte, connecte-toi, envoie-toi un message depuis un deuxième compte/navigateur

### À savoir sur le plan gratuit de Render
- Un service web gratuit **s'endort après une période d'inactivité** et met quelques secondes
  à se réveiller au prochain accès — normal, pas un bug.
- Une base PostgreSQL gratuite est **supprimée après un certain délai** (vérifie les conditions
  actuelles sur le site de Render) — pense à passer sur un plan payant si tu veux garder les données.

## Limites connues / simplifications par rapport à une app de production

- Le mode threading de Flask-SocketIO convient à une utilisation modérée ; pour une charge plus
  importante, il faudrait passer à un vrai serveur asynchrone (gevent/uvicorn) et plusieurs workers.
- Les photos uploadées (avatars, stories) sont stockées sur le disque du service Render — sur le plan
  gratuit, ce disque n'est **pas persistant** entre les redéploiements. Pour une vraie mise en
  production, il faudrait un stockage externe (S3, Cloudinary, Render Disk payant).

Dis-moi si tu veux que j'ajoute l'une de ces parties.
