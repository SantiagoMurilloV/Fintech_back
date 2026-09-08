# Fintech Close Agent — Blueprint

> Agente de cierre de mes basado en la arquitectura Tasman:
> **Playwright (CDP) + Claude Computer Use**, aplicada a operaciones fintech.
> Borrador para refinar en la reunión.

---

## 1. Problema

El cierre de mes en una operación fintech/contable exige tocar sistemas que **no tienen API
(o la tienen incompleta)**: portales bancarios, pasarelas de pago, plataformas contables,
portales de la DIAN. Hoy eso es trabajo manual: entrar a cada portal, descargar extractos,
cruzarlos contra los libros, perseguir diferencias y armar el paquete de cierre.

## 2. La solución (el patrón Tasman)

Tasman demostró una combinación que funciona en producción y que aquí reutilizamos tal cual:

| Capa           | Tecnología                                    | Rol                                                                                                                                                                                       |
| -------------- | --------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Sesión         | **Playwright `connect_over_cdp`**             | Se conecta al Chrome **real** del usuario ya autenticado. El humano hace login/2FA una vez; el agente hereda la sesión. Nunca guarda credenciales.                                        |
| Percepción     | **Claude Computer Use** (`computer_20250124`) | Mira screenshots y decide qué hacer cuando la UI es incierta: evaluar en qué estado está el portal, encontrar el botón de exportar, leer tablas visualmente. Resistente a cambios de UI.  |
| Acción crítica | **Playwright determinista**                   | Donde la fiabilidad importa (descargar archivo, poner un rango de fechas), selectores directos: rápido, barato, repetible. Lección Tasman: CU decide, Playwright ejecuta lo crítico.      |
| Memoria        | **Skills `.md` auto-aprendidas**              | Tras cada corrida exitosa el agente anota coordenadas y observaciones ("el botón Exportar está arriba a la derecha, x=1180,y=140"). Cada corrida es más barata y certera que la anterior. |
| Guardrails     | System prompt estricto                        | Lista blanca de zonas clicables por portal. NUNCA: transferir, pagar, cambiar configuración, navegar fuera del módulo permitido. Solo lectura/exportación.                                |
| Costos         | Token tracker                                 | Registro de tokens por llamada (CU es costoso; se mide todo).                                                                                                                             |

### El loop de Computer Use

```
1. Screenshot de la página
2. Se envía a Claude con el goal + skill notes previas
3. Claude devuelve acciones (click, type, scroll, key…)
4. Playwright ejecuta cada acción sobre el Chrome real
5. Nuevo screenshot como tool_result
6. Repite hasta que Claude responde texto plano (goal cumplido)
```

## 3. Aplicación fintech: pipeline de cierre de mes

```
┌────────────────────────────────────────────────────────────────────┐
│                    main.py --period 2026-07                        │
│                    (orquestador LangGraph)                         │
└──────┬─────────────────────┬─────────────────────┬─────────────────┘
       ▼                     ▼                     ▼
┌──────────────┐   ┌──────────────────┐   ┌──────────────────┐
│ FASE 1       │   │ FASE 2           │   │ FASE 3           │
│ RECOLECCIÓN  │──▶│ CONCILIACIÓN     │──▶│ PAQUETE CIERRE   │
│ (browser)    │   │ (determinista)   │   │ (outputs)        │
└──────────────┘   └──────────────────┘   └──────────────────┘

FASE 1 — Recolección (Playwright + Computer Use, patrón Tasman):
  Por cada portal configurado en portals.yaml:
    a. ensure_logged_in()  → CU evalúa el estado: ya logueado / pide login humano
    b. navigate_to_module() → CU encuentra la sección de movimientos/extractos
    c. set_period + export  → Playwright determinista si hay selector aprendido;
                              CU como fallback si la UI cambió
    d. download              → expect_download() de Playwright captura el archivo
    e. update_skill()        → anota lo aprendido para la próxima corrida

FASE 2 — Conciliación (100% determinista, sin LLM — dinero = cero alucinación):
  - ETL: normaliza extractos (xlsx/csv/pdf) a un esquema canónico de movimientos
  - Matching motor: banco vs libros (exacto → por monto+fecha±3d → por referencia)
  - Diferencias: partidas no conciliadas clasificadas (LLM solo ETIQUETA, nunca calcula)
  - Checklist de cierre: validaciones (saldos cuadran, sin huecos de fechas, etc.)

FASE 3 — Paquete de cierre:
  - Excel de conciliación por cuenta (matched / pendientes banco / pendientes libros)
  - PDF resumen ejecutivo del cierre
  - Notificación Slack con totales y diferencias que requieren humano
```

## 4. Estructura del proyecto

```
fintech_agent/
├── main.py                    # CLI: python main.py --period 2026-07 [--dry-run] [--portal X]
├── BLUEPRINT.md               # este documento
├── README.md
├── requirements.txt
├── .env.example
├── portals.yaml               # portales a recolectar: tipo, URL, módulo, formato export
├── config/
│   └── settings.py
├── browser/
│   ├── session.py             # CDP attach al Chrome real (patrón Tasman exacto)
│   ├── cu_client.py           # loop genérico de Computer Use (screenshot→acción→repeat)
│   └── portals/
│       ├── base_portal.py     # clase base híbrida: CU percepción + Playwright acción
│       ├── generic_bank.py    # driver genérico guiado por skills (cualquier banco)
│       └── alegra_portal.py   # contabilidad (API si hay, browser si no)
├── skills/
│   ├── skills_manager.py      # load/update de skills .md (Tasman)
│   └── <portal>/*.md          # skills por portal: find_export_button.md, set_date_range.md…
├── agent/
│   ├── llm_factory.py         # Claude = visión/CU · DeepSeek = nodos de texto baratos
│   └── prompts.py             # system prompts con guardrails por portal
├── close/                     # motor de cierre — determinista-first
│   ├── etl.py                 # extracto crudo → movimientos canónicos
│   ├── reconciliation.py      # motor de matching banco vs libros
│   ├── checks.py              # checklist de validaciones de cierre
│   └── models.py              # esquemas (Movement, MatchResult, CloseReport)
├── orchestrator/
│   └── graph.py               # LangGraph: recolectar → conciliar → reportar
├── outputs/
│   ├── excel_writer.py        # conciliación .xlsx
│   ├── pdf_generator.py       # resumen ejecutivo
│   └── slack_notifier.py
├── utils/
│   └── token_tracker.py       # (copiado de Tasman)
├── downloads/                 # extractos descargados por corrida
└── output/                    # paquete de cierre generado
```

## 5. Decisiones de diseño

1. **Dinero nunca pasa por el LLM.** La conciliación es aritmética determinista (pandas).
   El LLM solo: percibe UI (CU), extrae texto de screenshots, clasifica/etiqueta diferencias
   y redacta el resumen. Igual que financial-agent: determinista-first.
2. **CU decide, Playwright ejecuta.** Regla heredada de Tasman (`send_message`): las acciones
   con efecto (click de exportar, descarga) se hacen con selector Playwright cuando existe
   skill aprendida; CU solo cuando no hay ruta conocida. Esto baja costo y elimina el riesgo
   de que CU "improvise".
3. **Solo lectura.** El agente jamás ejecuta operaciones con efecto financiero en los
   portales. Guardrail duro en el system prompt + lista de módulos prohibidos por portal.
4. **Login humano.** Como Tasman: Chrome con `--remote-debugging-port=9222`, el humano se
   loguea (2FA incluido), el agente se conecta por CDP. Cero credenciales bancarias en código.
5. **Skills por portal.** Cada portal tiene su carpeta de skills. Un banco nuevo = crear
   entrada en `portals.yaml` + dejar que el agente aprenda la UI en la primera corrida asistida.
6. **LLM dual** (patrón hotel-booking-agent): Claude para todo lo visual (obligatorio para CU),
   DeepSeek vía `llm_factory` para nodos de texto (clasificar diferencias, redactar resumen).

## 6. Portales candidatos (a definir en la reunión)

| Portal                           | Vía                          | Qué se extrae                            |
| -------------------------------- | ---------------------------- | ---------------------------------------- |
| Bancolombia / banco X            | Browser (CU+PW)              | Extracto de movimientos del período      |
| Pasarela de pagos (Wompi/PayU/…) | Browser o API                | Liquidaciones y comisiones               |
| Alegra / Siigo                   | API si hay, browser fallback | Libro auxiliar de bancos, facturas       |
| DIAN                             | Browser (CU+PW)              | Estado de obligaciones (opcional fase 2) |

## 7. Fases de implementación

- **F0 (hoy, autónoma):** scaffolding completo + loop CU genérico + skills manager + motor
  de conciliación con datos sintéticos + outputs. Todo compila y el pipeline corre E2E
  con un portal de demo local.
- **F1 (post-reunión):** definir portales reales, primera corrida asistida por portal
  (aprendizaje de skills), ajustar ETL a los formatos reales de extracto.
- **F2:** checklist de cierre completo, DIAN, programación mensual automática.

## 8. Preguntas para la reunión

1. ¿Qué portales exactamente? ¿Cuántas cuentas bancarias?
2. ¿La contabilidad está en Alegra/Siigo/otro? ¿Hay API key disponible?
3. ¿Formato del paquete de cierre que espera el contador? ¿Quién lo recibe (Slack/email)?
4. ¿Tolerancias de matching (días de desfase, centavos por comisiones)?
5. ¿Máquina donde corre: la del analista (Chrome propio) o un equipo dedicado?
