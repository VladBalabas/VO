# Проєктування лабораторної роботи №3 (до реалізації)

Дата фіксації проєктування: 2026-10-24. Джерело вимог: `D:\UNI\мага 1\ВРО\Л3.docx`.

## 1. Варіант і межі роботи

- Варіант 2: локальний кластер `kind`, `R=5`, `requests.cpu=250m`, `limits.cpu=500m`, `limits.memory=512Mi`, ціль HPA 80 %, рівень стрибка `min(2.5*X(1), 0.9*X(5))`, додаткова вимога Д5.
- Розмір пулу PostgreSQL успадковується з лабораторної №2: `DB_POOL_SIZE=15`, без overflow.
- Compose-стенд і первинні результати лабораторної №2 не змінюються. Kubernetes-результати зберігаються окремо в `results/lab3_final`.
- Код предметної області й API не перепроєктовується. Додаються Kubernetes-інфраструктура, точна модель з'єднань генератора, збір кластерних метрик, аналіз еластичності та звіт.

## 2. Архітектура стенда

- `kind`: один control-plane і два worker. На control-plane ставиться мітка `role=infra`; стандартний taint control-plane зберігається.
- Host port 8080 спрямовується на NodePort web, host port 9090 — на NodePort Prometheus.
- PostgreSQL: StatefulSet на `role=infra`, headless Service, PVC, `max_connections=100`, Secret з `.env`. Ідемпотентну схему застосовує окремий Job на образі PostgreSQL.
- Web: Deployment без поля `replicas`, один Uvicorn-процес у контейнері, Service NodePort, ConfigMap з адресою БД і `DB_POOL_SIZE`, пароль через `secretKeyRef`, `POD_NAME` через Downward API. Readiness `/healthz`, liveness `/livez`, `preStop.sleep.seconds=5`, `terminationGracePeriodSeconds=15`.
- Monitoring на `role=infra`: Prometheus (scrape 5 s), kube-state-metrics, postgres-exporter; cAdvisor читається через kubelet. Metrics Server забезпечує `kubectl top` і HPA.
- HPA: CPU utilization 80 %, `minReplicas=1`, `maxReplicas=5`. Окремі маніфести/patch для scale-down stabilization 30 s та 300 s.

## 3. Визначення вимірювань

- Задана інтенсивність `z(t)`: кількість запланованих стартів запитів у секунду.
- Надіслана інтенсивність: кількість мережевих викликів, фактично розпочатих у секунду.
- Досягнута інтенсивність: кількість завершених 2xx-відповідей у секунду.
- Клієнтська latency починається у запланований момент і закінчується відповіддю/помилкою; вона містить schedule lag, очікування в черзі з'єднання та мережевий/серверний час.
- Timeout входить до `non_2xx` за методикою попередньої роботи й паралельно має окремий лічильник. Інші транспортні винятки рахуються як connection errors.
- Ступінь триває 30 s. `X` ступеня — середнє успішних завершень/s за останні 20 фактичних секунд ступеня. Розкид — вибіркове SD тих самих 20 посекундних значень.
- `X(N)` — найбільше `X` серед ступенів прогону N. Точка N є недосягнутою для USL, якщо останній ступінь і далі досягнув не менше 90 % заданої інтенсивності: насичення не було знайдено.
- p99 клієнта рахується за всіма завершеними спробами у тому самому 20-секундному вікні; p99 лише 2xx зберігається окремо. p99 сервера береться з Prometheus histogram за узгодженим вікном.
- Частка CPU throttling pod: приріст `container_cpu_cfs_throttled_periods_total` / приріст `container_cpu_cfs_periods_total` за те саме вікно.
- Дисбаланс `I=X_max/X_avg`, де X pod — успішні відповіді/s за `X-Pod` у вікні X(N); додатково звіряється з per-pod Prometheus counter.
- `s(t)` — ready replicas, інтегровані за фактичними timestamps Prometheus як кусочно-стала величина. `d(t)` — мінімальне N до R, для якого X(N)>=z(t); для невиміряного N=4 використовується USL-крива.
- Метрики еластичності `theta_U`, `theta_O`, `a_U`, `a_O`, `W` обчислюються посекундно за формулами завдання. Порушення p99 — секунда, де клієнтський p99 перевищує `2*p99_0`.
- Для HPA окремо вимірюються desired і ready replicas та чотири часові події кроку 11. Якщо подія не настала у відповідному відрізку, результат — `не досягнуто`.

## 4. Модель генератора

- У всіх прогонах ЛР3 існує рівно `8*R=40` логічних постійних HTTP-з'єднань.
- Кожне з'єднання обробляє запити послідовно, після 100 завершених спроб його session/connector закривається і створюється новий.
- Open-loop scheduler не чекає worker: заплановані запити стають у вимірювану чергу; latency починається з scheduled timestamp. Зберігаються queue/schedule lag, in-flight, queue depth і стан усіх 40 worker.
- Первинні записи пишуться потоково у JSON Lines, щоб довгі прогони не накопичувалися в пам'яті. Після завершення файл не змінюється; SHA-256 записується окремо.
- Перед серією генератор перевіряється на `/bench/noop` через той самий NodePort і ту саму модель 40 з'єднань. Значний lag/черга є обмеженням методики, а не продуктивністю сервісу.

## 5. План прогонів

1. Перевірка генератора на `/bench/noop`.
2. По одному безперервному ступінчастому прогону для N=1,2,3,5 без HPA.
3. Профіль без HPA на фіксованих 5 репліках: 30 s `0.3*X(1)`, 60 s spike, 90 s `0.3*X(1)`.
4. Основний профіль HPA з 30-секундною стабілізацією: та сама тривалість 30/60/90 s; до старту одна ready replica та desired=1 утримуються 60 s.
5. Д5 виконується окремо після основної серії: два HPA-прогони з вікнами 30 s і 300 s, кожний 30/60/360 s. Це не підміняє основний прогін кроку 11 і забезпечує однакове T для порівняння Д5.

Між незалежними прогонами: видалити HPA за потреби, задати потрібний scale, дочекатися стабільної кількості ready pod, нульової черги генератора та відсутності незавершених запитів. Старі запити не можуть переходити до наступної серії.

## 6. Raw і похідні артефакти

- `raw/*.jsonl`: один незмінний запис на HTTP-спробу.
- `samples/*.json`: часові знімки generator state, ready/desired replicas та Prometheus metrics із фактичними timestamps.
- `commands/*.json`: команда, UTC-час, exit code, stdout/stderr.
- `http/*.json`: метод, шлях, request body, HTTP status, повне response body й `X-Pod` для CRUD/інфраструктурних перевірок.
- `series/*.json`: компактні ступені та X(N), але не первинне джерело.
- `analysis.json`, `per_second/*.json`, `figures/*`: тільки відтворювані похідні дані.
- `independent_verification.json`: окремий перерахунок X, SD, дисбалансу, USL, метрик еластичності та SHA-256 без імпорту основного аналізатора.

## 7. Матриця вимога → доказ

| Вимога | Доказ |
| --- | --- |
| Кластер 1+2, port mappings, create/delete | cluster config, deploy/teardown commands, `kubectl get nodes -o wide` |
| PostgreSQL StatefulSet/PVC/headless/Secret/max_connections/Job | маніфести, sanitized object dump, Job log, SQL query |
| Дані після `kubectl delete pod` | HTTP create/read до і після та pod UID/timestamps |
| Web ConfigMap/Secret/Downward API/resources/probes/shutdown | маніфест, pod spec, endpoint log, termination events |
| Повний CRUD та X-Pod | журнал HTTP з request/response bodies і headers |
| Monitoring і Metrics Server | Prometheus targets/query responses, `kubectl top pods` |
| 40 connections і recycle/100 | generator metadata, per-connection counters, regression tests |
| N=1,2,3,5 та X(N) | immutable raw, Prometheus snapshots, compact series |
| Дисбаланс і per-pod p99/throttling | raw X-Pod + Prometheus, diagram per pod |
| USL Kubernetes і порівняння Compose | independent fit, common chart, explicit uncertainty |
| Fixed/HPA elasticity | profile raw, ready/desired/CPU series, equal-axis figures |
| Д5 30 s vs 300 s | два 450-секундні прогони з однаковим T і metric table |

## 8. Критерії завершення

- Усі команди відтворювані з чистого стану; пароль відсутній в image, ConfigMap, tracked-файлах і збережених sanitized logs.
- Кожне число звіту відтворюється з raw; raw не редагується аналізатором.
- Визначення метрик однакові у генераторі, аналізаторі, незалежній перевірці й звіті.
- Генератор не оголошується достатнім без `/bench/noop`, schedule lag, queue depth та connection-worker evidence.
- Причини bottleneck формулюються лише за прямими CPU/CFS/DB/queue даними.
- Якщо обов'язковий доказ відсутній або інструмент є лімітом, робота позначається неповною замість декларації успіху.
