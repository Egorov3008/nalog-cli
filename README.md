# nalog-cli

Неофициальный CLI + локальный REST API для [lknpd.nalog.ru](https://lknpd.nalog.ru/) — личного кабинета самозанятого (НПД).

Работает через прямые HTTP-запросы к API ФНС. Браузер нужен только один раз для получения токенов.

## Возможности

- Просмотр статуса авторизации и данных пользователя
- Список чеков за произвольный период (с пагинацией)
- Создание чека (физлицо / ЮЛ / ИП, наличные / расчётный счёт)
- Аннулирование чека
- Сводка по доходам и налогам
- Локальный REST API сервер для интеграций (n8n, Home Assistant, скрипты)

## Требования

- Python 3.8+ (только стандартная библиотека, без зависимостей)
- [Kimi WebBridge](https://www.kimi.com/products/kimi-webbridge) — нужен **только один раз** для `auth init`

## Первый запуск

1. Откройте [lknpd.nalog.ru](https://lknpd.nalog.ru/) в браузере и войдите в аккаунт
2. Запустите:

```bash
python3 nalog_cli.py auth init
```

Токены сохраняются в `~/.config/nalog-cli/tokens.json` (права 600). Браузер больше не нужен — токены обновляются автоматически.

## CLI

```bash
# Авторизация
python3 nalog_cli.py auth init          # первый запуск
python3 nalog_cli.py login-status       # проверить аккаунт

# Чеки
python3 nalog_cli.py receipts list
python3 nalog_cli.py receipts list --from 01.01.2026 --to 31.12.2026 --limit 50
python3 nalog_cli.py receipts summary

# Создать чек (физлицо)
python3 nalog_cli.py receipts add --name "Разработка сайта" --amount 5000

# Создать чек (юрлицо)
python3 nalog_cli.py receipts add \
  --name "Консультация" --amount 3000 \
  --client-type legal --client-inn 7701234567 --client-name "ООО Ромашка"

# Аннулировать
python3 nalog_cli.py receipts cancel --id <uuid>
python3 nalog_cli.py receipts cancel --id <uuid> --reason refund
```

Все команды выводят JSON: `{"ok": true, "data": ...}` или `{"ok": false, "error": ...}`.  
Ненулевой exit code при ошибке.

## REST API сервер

```bash
python3 nalog_cli.py serve --port 8080
```

| Метод | Путь | Описание |
|-------|------|----------|
| `GET` | `/api/login-status` | Данные пользователя |
| `GET` | `/api/receipts` | Список чеков (`?from=&to=&limit=&offset=&sort=`) |
| `GET` | `/api/receipts/summary` | Сводка по доходам |
| `POST` | `/api/receipts` | Создать чек |
| `POST` | `/api/receipts/<uuid>/cancel` | Аннулировать чек |

### Примеры

```bash
# Список чеков
curl http://localhost:8080/api/receipts?from=01.01.2026

# Создать чек
curl -X POST http://localhost:8080/api/receipts \
  -H 'Content-Type: application/json' \
  -d '{"name": "Разработка", "amount": 5000}'

# Аннулировать
curl -X POST http://localhost:8080/api/receipts/<uuid>/cancel \
  -H 'Content-Type: application/json' \
  -d '{"reason": "mistake"}'
```

Все ответы — JSON. CORS включён (`Access-Control-Allow-Origin: *`).

### Параметры POST /api/receipts

| Поле | Тип | Обязательно | По умолч. | Описание |
|------|-----|-------------|-----------|----------|
| `name` | string | ✓ | — | Наименование товара/услуги |
| `amount` | number | ✓ | — | Сумма за единицу (руб.) |
| `quantity` | integer | | 1 | Количество |
| `date` | string | | сегодня | Дата продажи (DD.MM.YYYY / YYYY-MM-DD) |
| `client_type` | string | | `individual` | `individual` или `legal` |
| `client_name` | string | | null | Наименование клиента (для ЮЛ/ИП) |
| `client_inn` | string | | null | ИНН клиента (для ЮЛ/ИП) |
| `payment_type` | string | | `cash` | `cash` или `account` |

## Токены

- Хранятся в `~/.config/nalog-cli/tokens.json` (chmod 600)
- Access token обновляется автоматически при получении 401
- Refresh token долгоживущий (месяцы); если истечёт — повторите `auth init`

## Дисклеймер

Неофициальный инструмент. Использует внутреннее API lknpd.nalog.ru, которое может измениться без предупреждения.
