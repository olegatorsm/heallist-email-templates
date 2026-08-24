# Экспорт разговора

## Пользователь

Напиши два скрпита на питоне: 1 для загрузки (pull) всех шаблонов design-manager из hubspot в локальную директорию, а второй на выгрузку (push) измененных шаблонов. API ключ у меня есть. Поищи актуальную документауию в интернете, как работать по api

---

## ChatGPT

Я сверю именно актуальные HubSpot CMS/Design Manager API и современную авторизацию, а затем дам два готовых скрипта: полный `pull` и `push` только изменённых файлов. Учту, что старые `hapikey` у HubSpot больше не являются нормальным способом авторизации.

---

## ChatGPT

Нашёл нужный API: `CMS Source Code API v3`. Он работает непосредственно с Developer File System, который виден в Design Manager; папку целиком скачать нельзя, поэтому `pull` должен рекурсивно обходить metadata и скачивать файлы по одному. Для `push` можно сравнивать локальные SHA-256 с манифестом, сохранённым после `pull`, и отправлять только изменившиеся файлы.

---

## ChatGPT

Ещё важный момент: на август 2026 у HubSpot уже есть новая date-versioned ветка API. Для новой интеграции лучше использовать `/cms/source-code/2026-03/...`, а не старый `/cms/v3/source-code/...`. Source Code API требует scope `content`; токен передаётся как `Authorization: Bearer ...`.

---

## ChatGPT

Да. Проверил актуальную документацию HubSpot на август 2026. Для Design Manager сейчас нужно использовать **CMS Source Code API** с date-versioned endpoint:

`https://api.hubapi.com/cms/source-code/2026-03/...`

Он работает с файлами Developer File System — то есть шаблонами, модулями, CSS/JS и другими файлами, видимыми в Design Manager. Папку одним запросом скачать нельзя: нужно получить metadata папки, пройти `children` рекурсивно и скачать каждый файл. ([developers.hubspot.com](https://developers.hubspot.com/docs/api-reference/latest/cms/source-code/guide?utm_source=chatgpt.com))

Я сделал два готовых standalone-скрипта:

[Скачать pull_hubspot_templates.py](sandbox:/mnt/data/pull_hubspot_templates.py)

[Скачать push_hubspot_templates.py](sandbox:/mnt/data/push_hubspot_templates.py)

### Как они работают

`pull_hubspot_templates.py` рекурсивно обходит Design Manager, скачивает шаблоны и создаёт рядом файл `.hubspot-manifest.json`. HubSpot metadata содержит в том числе `hash` файла, поэтому манифест сохраняет и HubSpot hash, и локальный SHA-256. ([developers.hubspot.com](https://developers.hubspot.com/docs/api-reference/latest/cms/source-code/get-file-metadata?utm_source=chatgpt.com))

По умолчанию шаблонами считаются `.html` вне директорий `*.module` плюс legacy `*.template.json`. То есть `xxx.module/module.html` не будет ошибочно принят за шаблон. Если нужно скачать вообще **весь Design Manager** — CSS, JS, модули, JSON, изображения и т. д. — есть `--all-assets`.

`push_hubspot_templates.py` затем сравнивает текущие SHA-256 с манифестом и отправляет только изменённые или новые файлы. Перед записью он дополнительно проверяет HubSpot `hash`: если файл после вашего `pull` кто-то поменял непосредственно в Design Manager, скрипт покажет `[conflict]` и не затрёт его без `--force`.

Кроме того, перед отправкой `.html`/`.json` вызывается официальный `/validate/{path}` endpoint, чтобы поймать ошибки HubL/JSON до загрузки. HubSpot сам рекомендует validation перед публикацией. ([developers.hubspot.com](https://developers.hubspot.com/docs/api-reference/latest/cms/source-code/validate-source-code-file?utm_source=chatgpt.com))

### Авторизация

Если под «API ключом» у вас имеется в виду **Private App Access Token**, всё хорошо. Нужен scope:

```text
content
```

Токен передаётся так:

```http
Authorization: Bearer <TOKEN>
```

Старый `hapikey` использовать не надо. Современная документация HubSpot использует OAuth или private app access tokens через Bearer Authorization. ([developers.hubspot.com](https://developers.hubspot.com/docs/apps/legacy-apps/authentication/intro-to-auth?utm_source=chatgpt.com))

В скриптах токен специально не хранится в исходнике. Задайте environment variable:

```bash
export HUBSPOT_ACCESS_TOKEN='pat-xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx'
```

В PowerShell:

```powershell
$env:HUBSPOT_ACCESS_TOKEN = "pat-xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
```

Установить зависимость:

```bash
python -m pip install requests
```

### 1. Скачать шаблоны

Я поставил `draft` режим по умолчанию, чтобы workflow был безопаснее:

```bash
python pull_hubspot_templates.py ./hubspot_templates
```

Явно:

```bash
python pull_hubspot_templates.py ./hubspot_templates --environment draft
```

Если нужны именно опубликованные версии:

```bash
python pull_hubspot_templates.py ./hubspot_templates --environment published
```

Чтобы скачать весь Developer File System, а не только шаблоны:

```bash
python pull_hubspot_templates.py ./hubspot_design_manager --all-assets
```

По умолчанию системная папка `@hubspot` пропускается. Если нужна и она:

```bash
python pull_hubspot_templates.py ./hubspot_templates --include-hubspot-defaults
```

HubSpot официально подтверждает, что `/` представляет корень Developer File System; их CLI также позволяет скачать весь filesystem от корня. ([developers.hubspot.com](https://developers.hubspot.com/docs/cms/start-building/introduction/developer-environment/github-integration?utm_source=chatgpt.com))

После pull структура будет примерно такой:

```text
hubspot_templates/
├── my-theme/
│   ├── templates/
│   │   ├── home.html
│   │   ├── landing.html
│   │   └── blog.html
│   └── partials/
│       ├── header.html
│       └── footer.html
└── .hubspot-manifest.json
```

### 2. Посмотреть, что будет отправлено

После изменения файлов:

```bash
python push_hubspot_templates.py ./hubspot_templates --dry-run
```

Например:

```text
Changed/new files: 2
[dry-run] my-theme/templates/home.html
[dry-run] my-theme/templates/landing.html

Done. Uploaded: 0; conflicts: 0; validation failures: 0
Dry run: HubSpot was not modified.
```

### 3. Отправить изменённые

```bash
python push_hubspot_templates.py ./hubspot_templates
```

Получите примерно:

```text
Changed/new files: 2
[push] my-theme/templates/home.html
[push] my-theme/templates/landing.html

Done. Uploaded: 2; conflicts: 0; validation failures: 0
```

После каждого успешного upload манифест обновляется, поэтому повторный запуск:

```bash
python push_hubspot_templates.py ./hubspot_templates
```

даст:

```text
No local changes to push.
```

### Draft и Published

Это особенно важно. HubSpot имеет два environment:

```text
draft       — неопубликованные изменения
published   — live-версия
```

Загрузка непосредственно в `published` эквивалентна нажатию **Publish** в Design Manager; HubSpot отдельно предупреждает, что при этом текущий draft очищается. ([developers.hubspot.com](https://developers.hubspot.com/docs/api-reference/latest/cms/source-code/guide?utm_source=chatgpt.com))

Поэтому рекомендованный цикл здесь такой:

```bash
# 1. Получаем draft
python pull_hubspot_templates.py ./hubspot_templates --environment draft

# 2. Редактируем файлы

# 3. Проверяем
python push_hubspot_templates.py ./hubspot_templates --dry-run

# 4. Отправляем обратно в draft
python push_hubspot_templates.py ./hubspot_templates
```

Если осознанно хотите публиковать прямо на production:

```bash
python push_hubspot_templates.py ./hubspot_templates \
    --environment published \
    --force
```

Скрипт дополнительно напечатает предупреждение про `PUBLISHED`.

Удаление локального файла **не удаляет** его из HubSpot — я специально не добавлял автоматический delete, чтобы случайный `rm`/git checkout не снёс live-шаблон. Для удаления у HubSpot есть отдельный `DELETE .../content/{path}` endpoint. ([developers.hubspot.com](https://developers.hubspot.com/docs/api-reference/latest/cms/source-code/delete-source-code-file?utm_source=chatgpt.com))

Ещё одна деталь: современные HubSpot coded templates действительно являются `.html` + HubL и имеют `templateType` annotations; модули при локальной разработке представлены отдельными `.module` директориями. Это и заложено в фильтрацию pull/push. ([developers.hubspot.com](https://developers.hubspot.com/docs/cms/start-building/building-blocks/templates/html-hubl-templates?utm_source=chatgpt.com))

---

## Пользователь

export all conversation in markdown
