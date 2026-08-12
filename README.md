# Rat King

Rat King compare automatiquement la rentabilité des récompenses Kingdom dans
les restaurants Burger King France. La page d’accueil est neutre : chaque
visiteur recherche un établissement par ville, code postal, nom, adresse ou
identifiant public, puis consulte son propre lien de comparaison.

Le service utilise uniquement les pages et fichiers publics accessibles sans
compte. Il ne demande aucun identifiant, ne lit aucun cookie utilisateur,
n’appelle aucun endpoint privé et ne tente jamais de contourner une protection
technique.

## Premier démarrage

`rat-king serve` ouvre le serveur HTTP immédiatement, puis récupère en arrière-
plan l’annuaire public et les récompenses publiques.

- `/healthz` répond 200 tant que le processus fonctionne ;
- `/readyz` répond 503 jusqu’au premier snapshot partagé valide ;
- `/` affiche un sélecteur neutre, y compris pendant l’initialisation ;
- en cas d’indisponibilité BK, le processus reste vivant et réessaie après 30,
  60, 120 secondes, avec un plafond de 15 minutes ;
- après un premier succès, une panne conserve tous les derniers snapshots
  valides.

Aucun catalogue n’est téléchargé avant qu’un visiteur choisisse un restaurant.
Le premier accès affiche une page d’attente pendant la préparation asynchrone.
Le navigateur sonde uniquement Rat King et recharge automatiquement la page
quand le comparateur est prêt.

## Recherche et confidentialité

La recherche travaille exclusivement sur l’annuaire déjà stocké localement.
Elle normalise accents et ponctuation, privilégie les correspondances exactes
ou préfixes et tolère une faute simple dans un nom de ville suffisamment long.
Cette tolérance ne s’applique jamais au matching des récompenses.

Le choix est porté par une URL `/restaurants/KNNNN` : il ne modifie aucun état
global et n’utilise ni cookie, ni session, ni stockage navigateur. Rat King ne
persiste pas les recherches, choix ou adresses IP. Les logs applicatifs ne
contiennent ni URL complète, ni chaîne de requête, ni identifiant sélectionné.

## Politesse réseau et quotas

- User-Agent explicite, timeout et trois tentatives maximum ;
- réponses publiques limitées à 16 Mio après décompression ;
- backoff borné et requêtes conditionnelles ETag/Last-Modified ;
- cookies de réponse supprimés immédiatement et jamais persistés ;
- une seule tâche de catalogue à la fois ;
- une entrée en file par restaurant ;
- file limitée à 10 restaurants ;
- six nouveaux chargements à froid par fenêtre glissante d’une heure ;
- fraîcheur et intervalle partagé par défaut de six heures ;
- cache LRU limité à 20 restaurants.
- serveur HTTP limité à 32 requêtes simultanées et sockets clientes bornées à
  10 secondes.

Une recherche ou une lecture d’état ne déclenche aucun appel BK. Seule la page
d’un identifiant présent dans l’annuaire validé peut demander une admission en
file, sous toutes les limites ci-dessus. Il n’existe aucun endpoint public de
refresh forcé.

## Sources publiques

- [liste des restaurants](https://ecoceabkstorageprdnorth.blob.core.windows.net/static/restaurants.json) ;
- catalogue SSR public construit avec un identifiant validé ;
- [paliers Kingdom](https://www.burgerking.fr/paliers-kingdom) ;
- [CGU fidélité publiques](https://www.burgerking.fr/page/cgu-fidelite), en
  fallback si la page des paliers est incomplète.

Les prix sont extraits de la page SSR publique. Rat King n’appelle jamais les
routes privées et n’essaye pas de reproduire une authentification.

## Installation locale

Prérequis : Python 3.12 ou 3.13.

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install ".[dev]"
export BK_STATE_DIR="$PWD/.local-data"
export BK_OUTPUT_DIR="$BK_STATE_DIR/site"
rat-king serve --host 127.0.0.1 --port 8080 --refresh-interval 21600
```

Ouvrir ensuite <http://127.0.0.1:8080/> et chercher une ville. Au démarrage, le
pod ou le processus charge immédiatement les sources partagées si `/data` est
vide.

### Commandes

- `rat-king serve` sert le site, initialise les sources et traite la file ;
- `rat-king refresh` actualise les sources partagées puis les entrées du cache
  devenues anciennes ;
- `rat-king refresh --restaurant KNNNN` actualise explicitement un identifiant
  strict après vérification dans l’annuaire ;
- `rat-king check` valide hors ligne le snapshot partagé et tous les snapshots
  de restaurants.

`SIGUSR1` réveille le scheduler partagé et propose les entrées anciennes au
coordinateur sans contourner les quotas. `SIGTERM` arrête les admissions,
annule coopérativement la requête active et ferme le serveur proprement.

## Configuration

| Variable | Défaut | Rôle |
|---|---:|---|
| `BK_REFRESH_INTERVAL_SECONDS` | `21600` | Fraîcheur et intervalle partagé |
| `BK_OUTPUT_DIR` | `/data/site` | Lien vers l’accueil partagé courant |
| `BK_STATE_DIR` | `/data` | Sources, snapshots, statuts et LRU |
| `BK_HTTP_TIMEOUT_SECONDS` | `30` | Timeout explicite par requête |
| `BK_CACHE_MAX_RESTAURANTS` | `20` | Nombre maximal de restaurants en cache |
| `BK_REFRESH_QUEUE_MAX` | `10` | Capacité de la file catalogues |
| `BK_COLD_LOADS_PER_HOUR` | `6` | Admissions à froid par heure glissante |
| `BK_SEARCH_MAX_RESULTS` | `20` | Suggestions locales maximales |
| `PORT` | `8080` | Port HTTP par défaut |

Les valeurs numériques sont bornées. Aucune variable n’accepte une URL ou un
hôte distant, ce qui empêche de transformer le service en proxy SSRF.

## Données et atomicité

```text
/data/
├── cache-index.json
├── site -> shared/snapshots/<id>/site
├── shared/
│   ├── current.json
│   ├── status.json
│   └── snapshots/<id>/
│       ├── raw/
│       ├── normalized.json
│       └── site/
└── restaurants/
    └── KNNNN/
        ├── current.json
        ├── status.json
        └── snapshots/<id>/
            ├── raw/catalogue.html
            ├── normalized.json
            └── site/{index.html,data.json}
```

Chaque refresh écrit dans un répertoire temporaire, valide entièrement les
données, calcule le rapport, génère le site puis remplace atomiquement un petit
pointeur `current.json`. Un snapshot vide, partiel ou incohérent ne remplace
jamais la dernière version valide. Deux générations par entrée sont conservées.

## Calculs et transparence

Pour un prix public en euros et un palier en Couronnes :

```text
euros_par_couronne = prix_euros / couronnes
depense_requise    = couronnes / 2
rendement_pct      = 200 * prix_euros / couronnes
```

Les menus ordinaires utilisent la taille M et les menus King Size la taille L.
Les produits seuls et box utilisent `prices.alone`. Une récompense floue ou
ambiguë n’est jamais acceptée silencieusement : elle apparaît dans la section
diagnostique. La domination compare seulement des quantités substituables d’une
même famille et rappelle que deux retraits nécessitent deux commandes.

## HTTP

- `/` : recherche neutre ;
- `/api/restaurants?q=...` : suggestions locales ;
- `/restaurants/KNNNN` : comparateur ou attente ;
- `/api/restaurants/KNNNN/status` : état local sans refresh ;
- `/restaurants/KNNNN/data.json` : données déjà publiées ;
- `/healthz` et `/readyz` : probes ;
- `/api/status` : état global agrégé sans sélection utilisateur.

Les pages utilisent l’auto-échappement Jinja2. CSS et JavaScript sont servis par
Rat King ; consulter une page ne charge aucune ressource BK.

## Docker

```bash
docker build -t rat-king:0.2.2 .
docker volume create rat-king-data
docker run --rm --name rat-king \
  --read-only \
  --volume rat-king-data:/data \
  --publish 8080:8080 \
  rat-king:0.2.2
```

L’image utilise Python 3.14 sur Debian 13 « Trixie », avec une base officielle
épinglée par digest. Elle installe une wheel, s’exécute avec l’UID/GID 10001 et
n’écrit que dans `/data`.

Un push du tag correspondant à la version du paquet, par exemple `v0.2.2`,
déclenche la CI complète puis publie `ghcr.io/lac-coloc/rat-king:0.2.2`,
`latest` et un tag immuable lié au SHA Git. Aucun push de branche ou de pull
request ne publie d’image.

## Kubernetes

```bash
kubectl apply -f deploy/k8s/deployment.yaml
kubectl apply -f deploy/k8s/service.yaml
kubectl port-forward service/rat-king 8080:80
```

Le Deployment référence l’image GHCR par digest immuable. Le package conteneur
est public et ne nécessite aucun secret. Si un fork rend son package privé,
créer un secret de registre dans le namespace par le mécanisme de gestion de
secrets du cluster, puis ajouter sous `spec.template.spec` :

```yaml
imagePullSecrets:
  - name: ghcr-pull
```

Ce secret est une configuration du cluster et ne doit jamais être commité dans
ce dépôt.

Le Deployment d’exemple possède une réplique, une racine en lecture seule, un
utilisateur non-root, seccomp `RuntimeDefault`, aucune capability, aucun token
de ServiceAccount, des probes HTTP et des ressources bornées. `/data` est un
`emptyDir` de 256 Mio : il survit au redémarrage du conteneur dans le même pod,
mais pas au remplacement du pod.

Pour conserver le cache entre remplacements, remplacer uniquement le volume :

```yaml
volumes:
  - name: data
    persistentVolumeClaim:
      claimName: rat-king-data
```

Le PVC doit accepter l’écriture par le groupe 10001. Garder une seule réplique
par volume : la file et le LRU sont locaux au processus.

## Tests et CI

```bash
ruff format --check .
ruff check .
pytest
python -m build
```

Les tests normaux et la CI n’utilisent jamais le réseau BK. Le smoke public est
facultatif et choisit dynamiquement une entrée de l’annuaire :

```bash
BK_LIVE_TEST=1 pytest tests/test_live.py -v
```

La CI vérifie format, lint, confidentialité des artefacts, tests hors réseau,
build du paquet et build Docker.
