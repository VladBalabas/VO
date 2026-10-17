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

    with response:
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
    instance: str | None = None
    completed = False

    def checked_request(
        method: str,
        path: str,
        payload: dict[str, object] | None = None,
    ) -> tuple[int, object | None, dict[str, str]]:
        status, body, response_headers = request(method, path, payload)
        check = {
            "request": f"{method} {path}",
            "method": method,
            "path": path,
            "payload": payload,
            "status": status,
            "body": body,
            "headers": response_headers,
        }
        checks.append(check)
        print(json.dumps(check, ensure_ascii=False, indent=2), flush=True)
        return status, body, response_headers

    try:
        status, body, _ = checked_request("GET", "/healthz")
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
        status, body, headers = checked_request("POST", "/referrals", create_payload)
        require(status == 201, f"POST /referrals повернув код {status}, очікувався 201")
        require(isinstance(body, dict), "POST /referrals не повернув JSON-об'єкт")
        referral_id = body.get("id")
        require(isinstance(referral_id, int), "У створеного направлення немає числового id")
        require("x-pod" in headers, "У відповіді немає заголовка X-Pod")
        instance = headers["x-pod"]

        offset = 0
        while True:
            status, body, _ = checked_request(
                "GET", f"/referrals?limit=100&offset={offset}&status=created"
            )
            require(status == 200, f"GET /referrals повернув код {status}, очікувався 200")
            require(isinstance(body, dict), "GET /referrals не повернув JSON-об'єкт")
            items = body.get("items")
            total = body.get("total")
            require(isinstance(items, list), "У відповіді переліку немає масиву items")
            require(isinstance(total, int), "У відповіді переліку немає числового total")
            if any(item.get("id") == referral_id for item in items):
                break
            offset += len(items)
            require(
                bool(items) and offset < total,
                "Створене направлення не знайдено у відфільтрованому списку",
            )

        status, body, _ = checked_request("GET", f"/referrals/{referral_id}")
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
        status, body, _ = checked_request(
            "PUT", f"/referrals/{referral_id}", replace_payload
        )
        require(status == 200, f"PUT /referrals/{{id}} повернув код {status}")
        require(
            isinstance(body, dict)
            and body.get("modality") == "CT"
            and body.get("status") == "scheduled",
            "Направлення не було коректно оновлено",
        )

        invalid_payload = dict(replace_payload)
        invalid_payload["modality"] = "INVALID"
        status, _, _ = checked_request("POST", "/referrals", invalid_payload)
        require(status == 400, f"Некоректні дані повернули код {status}, очікувався 400")

        payload_with_id = dict(create_payload)
        payload_with_id["id"] = 999999
        status, _, _ = checked_request("POST", "/referrals", payload_with_id)
        require(status == 400, f"Переданий клієнтом id повернув код {status}, очікувався 400")

        for boundary_id in (1, 2**63 - 1):
            status, _, _ = checked_request("GET", f"/referrals/{boundary_id}")
            require(
                status in (200, 404),
                f"Допустимий id {boundary_id} повернув код {status}, очікувався 200 або 404",
            )

        for invalid_id in (0, -1, 2**63, -(2**63) - 1, "abc"):
            for method in ("GET", "PUT", "DELETE"):
                payload = replace_payload if method == "PUT" else None
                status, body, _ = checked_request(
                    method, f"/referrals/{invalid_id}", payload
                )
                require(
                    status == 400,
                    f"{method} з недопустимим id {invalid_id} повернув код {status}, очікувався 400",
                )
                require(
                    isinstance(body, dict) and "detail" in body,
                    "Відповідь перевірки id не містить опису помилки",
                )

        status, _, _ = checked_request("DELETE", f"/referrals/{referral_id}")
        require(status == 204, f"DELETE /referrals/{{id}} повернув код {status}")
        deleted = True

        status, _, _ = checked_request("DELETE", f"/referrals/{referral_id}")
        require(status == 404, f"Повторний DELETE повернув код {status}, очікувався 404")

        status, _, _ = checked_request("GET", f"/referrals/{referral_id}")
        require(status == 404, f"Видалений запис повернув код {status}, очікувався 404")

        completed = True
        print("Автоматична перевірка API успішно завершена.")
        print(f"Запит обробив контейнер: {instance}")
    finally:
        try:
            if referral_id is not None and not deleted:
                checked_request("DELETE", f"/referrals/{referral_id}")
        finally:
            if OUTPUT_PATH:
                Path(OUTPUT_PATH).write_text(
                    json.dumps(
                        {
                            "generated_at": datetime.now(UTC).isoformat(),
                            "base_url": BASE_URL,
                            "completed": completed,
                            "checks": checks,
                            "instance": instance,
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )


if __name__ == "__main__":
    main()
