# Первинні raw-дані лабораторної №3

Файли `*.jsonl` є незмінюваними записами кожної HTTP-спроби й локально виключені
з Git через розмір. Їхні компактні summaries та цей маніфест версіонуються.

| Прогін | Записів | Розмір, байт | SHA-256 |
| --- | ---: | ---: | --- |
| `noop` | 548000 | 218640566 | `cef16c322551201fe389193835e212086af81c3b15007c8204d895be6d1165d6` |
| `n1` | 38000 | 15027359 | `07b681ec009c27b383cc2bd33ad7d370ef134edf4b2f57a15ef8e4633dfcee80` |
| `n2` | 92000 | 36576559 | `8ed638486bfb1cf8c1c55812b032110ad5cd7ad0758eed7bef6233efb3f888e8` |
| `n3` | 128000 | 50973212 | `2052111c0335619bd548a8d779ee550e82c7a3f3f8cf0d48ed1ca61b7e2a09b6` |
| `n5` | 218000 | 86945299 | `7492dd67c5b25010af1fbcad9271a462cd6226a67bd32a65a94b0b11aaf6760b` |
| `fixed_profile` | 60755 | 24113044 | `3c5941c8c694eca245cc796091db22690f4ab26933d503cdfc22ded540600998` |
| `hpa30_profile` | 60755 | 24062439 | `5b0b2d4707b6b55a79266a92760e58d2f307429a97f11681de80bd48076bc9df` |
| `d5_hpa30` | 87214 | 34653993 | `16450e88379b804b82840d9fbcde14d0b330ad837f602d5149319db7133f0fdc` |
| `d5_hpa300` | 87214 | 34399254 | `068044c057146ed7abcdc8f3926fc5b06d154e9fcfe899a11cd6c5fe8368b584` |

## Відтворення і перевірка

```powershell
.\.venv\Scripts\python.exe scripts\run_lab3.py --phase stairs
.\.venv\Scripts\python.exe scripts\run_lab3.py --phase profiles
.\.venv\Scripts\python.exe scripts\analyze_lab3.py
.\.venv\Scripts\python.exe scripts\verify_lab3_results.py
```

Незалежна перевірка повторно читає кожен JSONL, звіряє SHA-256 і кількість записів,
перераховує X, SD, pod-розподіл, USL та метрики еластичності.
