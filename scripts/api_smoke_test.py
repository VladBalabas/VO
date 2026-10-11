from __future__ import annotations

import json
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4


BASE_URL = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "http://proxy"
OUTPUT_PATH = sys.argv[2] if len(sys.argv) > 2 else None


def request(
    method: str,
    path: str,
    payload: dict[str, object] | None = None,
) -> tuple[int, object | None, dict[str, str]]:
    body = None
    headers = {"Accept": "application/json"}

    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    http_request = Request(
        f"{BASE_URL}{path}",
        data=body,
        headers=headers,
        method=method,
    )

    try:
        response = urlopen(http_request, timeout=10)
    except HTTPError as error:
        response = error

    raw_body = response.read()
    parsed_body = json.loads(raw_body) if raw_body else None
    response_headers = {
        key.lower(): value for key, value in response.headers.items()
    }
    return response.status, parsed_body, response_headers


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main() -> None:
    referral_id: int | None = None
    deleted = False
    patient_code = f"AUTO-{uuid4().hex[:8].upper()}"
    checks: list[dict[str, object]] = []

    try:
        status, body, _ = request("GET", "/healthz")
        checks.append({"request": "GET /healthz", "status": status})
        require(status == 200, f"/healthz повернув код {status}, очікувався 200")
        require(body == {"status": "готовий"}, "Неочікуване тіло відповіді /healthz")

        create_payload: dict[str, object] = {
            "patient_code": patient_code,
            "modality": "MRI",
            "priority": 2,
            "doctor": "Автоматичний тест",
            "issued_at": date.today().isoformat(),
            "status": "created",
        }
        status, body, headers = request("POST", "/referrals", create_payload)
        checks.append({"request": "POST /referrals", "status": status})
        require(status == 201, f"POST /referrals повернув код {status}, очікувався 201")
        require(isinstance(body, dict), "POST /referrals не повернув JSON-об'єкт")
        referral_id = body.get("id")
        require(isinstance(referral_id, int), "У створеного направлення немає числового id")
        require("x-pod" in headers, "У відповіді немає заголовка X-Pod")

        status, body, _ = request(
            "GET", "/referrals?limit=20&offset=0&status=created"
        )
        checks.append({"request": "GET /referrals", "status": status})
        require(status == 200, f"GET /referrals повернув код {status}, очікувався 200")
        require(isinstance(body, dict), "GET /referrals не повернув JSON-об'єкт")
        items = body.get("items", [])
        require(
            any(item.get("id") == referral_id for item in items),
            "Створене направлення не знайдено у відфільтрованому списку",
        )

        status, body, _ = request("GET", f"/referrals/{referral_id}")
        checks.append({"request": "GET /referrals/{id}", "status": status})
        require(status == 200, f"GET /referrals/{{id}} повернув код {status}")
        require(
            isinstance(body, dict) and body.get("patient_code") == patient_code,
            "Отримане направлення не збігається зі створеним",
        )

        replace_payload: dict[str, object] = {
            "patient_code": patient_code,
            "modality": "CT",
            "priority": 1,
            "doctor": "Оновлений тестовий лікар",
            "issued_at": date.today().isoformat(),
            "status": "scheduled",
        }
        status, body, _ = request(
            "PUT", f"/referrals/{referral_id}", replace_payload
        )
        checks.append({"request": "PUT /referrals/{id}", "status": status})
        require(status == 200, f"PUT /referrals/{{id}} повернув код {status}")
        require(
            isinstance(body, dict)
            and body.get("modality") == "CT"
            and body.get("status") == "scheduled",
            "Направлення не було коректно оновлено",
        )

        invalid_payload = dict(replace_payload)
        invalid_payload["modality"] = "INVALID"
        status, _, _ = request("POST", "/referrals", invalid_payload)
        checks.append({"request": "POST /referrals (невалідні дані)", "status": status})
        require(status == 400, f"Некоректні дані повернули код {status}, очікувався 400")

        payload_with_id = dict(create_payload)
        payload_with_id["id"] = 999999
        status, _, _ = request("POST", "/referrals", payload_with_id)
        checks.append({"request": "POST /referrals (id від клієнта)", "status": status})
        require(status == 400, f"Переданий клієнтом id повернув код {status}, очікувався 400")

        status, _, _ = request("DELETE", f"/referrals/{referral_id}")
        checks.append({"request": "DELETE /referrals/{id}", "status": status})
        require(status == 204, f"DELETE /referrals/{{id}} повернув код {status}")
        deleted = True

        status, _, _ = request("DELETE", f"/referrals/{referral_id}")
        checks.append({"request": "DELETE /referrals/{id} повторно", "status": status})
        require(status == 404, f"Повторний DELETE повернув код {status}, очікувався 404")

        status, _, _ = request("GET", f"/referrals/{referral_id}")
        checks.append({"request": "GET /referrals/{id} після видалення", "status": status})
        require(status == 404, f"Видалений запис повернув код {status}, очікувався 404")

        print("Автоматична перевірка API успішно завершена.")
        print(f"Запит обробив контейнер: {headers['x-pod']}")
        if OUTPUT_PATH:
            Path(OUTPUT_PATH).write_text(
                json.dumps(
                    {
                        "generated_at": datetime.now(UTC).isoformat(),
                        "checks": checks,
                        "instance": headers["x-pod"],
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
    finally:
        if referral_id is not None and not deleted:
            request("DELETE", f"/referrals/{referral_id}")


if __name__ == "__main__":
    main()
