# Visión General del Proyecto PBScaler

Este documento ofrece una vista de alto nivel del proyecto tras la lectura línea a línea de cada archivo fuente. Su objetivo es servir como mapa mental para entender qué hace cada parte, cómo se conectan entre sí y cuál es el flujo de control. Evita detalles de implementación salvo cuando son imprescindibles para comprender una decisión de diseño.

---

## 1. ¿Qué es PBScaler?

PBScaler es un **framework de autoescalado consciente de cuellos de botella** para aplicaciones de microservicios desplegadas en Kubernetes (publicado en IEEE TSC 2024). Su idea central es que escalar indiscriminadamente todos los servicios frente a violaciones de SLO desperdicia recursos; en cambio, conviene:

1. **Detectar** violaciones de SLO mediante pruebas estadísticas sobre latencias p90.
2. **Identificar** los servicios raíz del problema con PageRank sobre el grafo de dependencias.
3. **Decidir** cuántas réplicas asignar a esos pocos servicios mediante un Algoritmo Genético que usa un modelo Random Forest preentrenado como función de aptitud.
4. **Aplicar** el resultado mediante la API de Kubernetes.

El proyecto también incluye baselines para comparación (KHPA, MicroScaler, SHOWAR, Random) y una rama experimental de Aprendizaje por Refuerzo.

**Entornos objetivo:** Kubernetes 1.20.4+, Istio 1.13.4+, Prometheus como fuente de métricas.

---

## 2. Arquitectura de Alto Nivel

```
config.yaml
    ↓
Config ───────────────┐
                      ↓
Prometheus → PrometheusClient → PBScaler ──► Detección de anomalías (t-test)
                                        ──► Análisis de causa raíz (PageRank)
                                        ──► Optimización GA (fitness con RF)
                                        ↓
                                  KubernetesClient → escala deployments
                                        ↓
                                  MetricCollect → CSVs en output/
```

El sistema se apoya en **dos bucles concurrentes** dentro de `PBScaler.py`:

- **Bucle de anomalías** (cada 15 s): consulta latencias y dispara escalado hacia arriba cuando detecta violaciones de SLO estadísticamente significativas.
- **Bucle de desperdicio** (cada 120 s): compara QPS actual vs. histórico para identificar servicios sobredimensionados y escalarlos hacia abajo.

---

## 3. Punto de Entrada y Configuración

### `main.py`
Punto de entrada único. Implementa un *factory* (`initController`) que instancia el controlador elegido (por defecto `PBScaler`). Tras `controller.start()`, invoca `MetricCollect.collect()` para exportar métricas a CSV. El controlador activo está fijo en el código; cambiar de baseline requiere editar el archivo.

### `config.yaml`
Archivo único de configuración con todos los parámetros de ejecución: namespace de Kubernetes, kubeconfig, endpoints de Prometheus, SLO objetivo (200 ms), cotas de réplicas (min_pod, max_pod), duración del experimento, ruta al modelo Random Forest y directorio de salida.

### `config/Config.py`
Carga `config.yaml`, resuelve rutas relativas contra la raíz del proyecto y permite sobrescribir valores críticos mediante variables de entorno (`K8S_NAMESPACE`, `K8S_CONFIG`, `PROM_RANGE_URL`, `PROM_QUERY_URL`). Calcula los timestamps de inicio y fin del experimento.

---

## 4. Núcleo del Algoritmo

### `PBScaler.py`
Orquesta el algoritmo completo. Mantiene dos tareas periódicas (detección de anomalías y detección de desperdicio). Cuando detecta violaciones:

- Construye un **subgrafo de llamadas anómalas** ponderando aristas por correlación de Pearson entre series de latencia.
- Calcula **potencial topológico** por servicio (combinando conteo de fallos y anomalías de predecesores).
- Aplica **PageRank personalizado** para jerarquizar candidatos a cuello de botella.
- Selecciona el **top-K=2** servicios aún por debajo de `max_pod`.
- Lanza el **GA** para encontrar el vector óptimo de réplicas.
- Aplica el escalado mediante la API de Kubernetes.

Constantes clave: `CONF=0.05` (umbral p-value), `ALPHA=0.2` (holgura sobre SLO), `BETA=0.9` (umbral de desperdicio), `K=2` (top-K cuellos de botella). Los logs usan prefijos estructurados (`INIT:`, `ANOMALY:`, `PAGERANK:`, `GA_OPT:`, `GA_FITNESS:`, `WASTE:`, `SCALE:`) para facilitar el análisis posterior.

### `util/GA.py`
Envuelve el Algoritmo Genético con `pymoo`. Define un problema de maximización de una sola objetivo cuya aptitud combina:

- **R1** — recompensa de cumplimiento de SLO predicha por el modelo Random Forest.
- **R2** — recompensa de eficiencia de costo (`1 - sum(réplicas)/sum(cotas_max)`).

La aptitud final es `0.5·R1 + 0.5·R2`. Usa *Simulated Binary Crossover* (SBX), mutación polinomial, muestreo entero y un *repair* que redondea a enteros. Tamaño de población: 50. Generaciones: 5. Registra la mejor y la media por generación.

---

## 5. Capa de Utilidades (`util/`)

| Módulo | Rol |
|--------|-----|
| `PrometheusClient.py` | Cliente de consultas PromQL. Extrae latencias p50/p90/p99 por arista de llamada y por servicio, QPS, CPU, memoria y tasa de éxito. Construye grafos dirigidos de dependencias. Filtra servicios sin estado (redis, mongo, mysql, rabbitmq, etc.). |
| `KubernetesClient.py` | Cliente de la API de Kubernetes. Descubre deployments, filtra servicios con estado, consulta réplicas actuales y aplica `patch_scale()`. Puede invocar `kubectl` para aplicar manifests. |
| `PCAUtil.py` | Pequeñas utilidades de Análisis de Componentes Principales. Uso exploratorio; no forma parte del camino caliente. |
| `Spectrum.py` | Fórmulas clásicas de *Spectrum-Based Fault Localization* (Tarantula, Ochiai, Jaccard, etc.). Actualmente no se usa en el núcleo; puede ser código residual o reservado para análisis extendido de causa raíz. |

---

## 6. Monitoreo Post-Experimento

### `monitor/MetricCollect.py`
Tras la ejecución, consulta Prometheus para el rango de tiempo completo del experimento y exporta ocho CSVs: latencias por llamada, latencias por servicio, uso de CPU/memoria, tasa de éxito, QPS, métricas detalladas por pod, y conteo de réplicas. Habilita el análisis offline y la comparación entre ejecuciones.

---

## 7. Controladores Baseline (`others/`)

Estos controladores permiten comparar PBScaler contra enfoques alternativos:

- **`KHPA.py`** — Envoltorio del HPA nativo de Kubernetes. Ejecuta scripts de shell para aplicar manifests HPA.
- **`MicroScaler.py`** — Optimización Bayesiana por servicio. Detecta violaciones vía ratio `p50/p90`, clasifica como escalado-in o escalado-out, y usa `BayesianOptimization` para elegir réplicas.
- **`Showar.py`** — Controlador PID consciente de topología. Un PID por servicio, con escalado porcentual respetando dependencias y esperando convergencia de hijos antes de escalar padres.
- **`RandomController.py`** — Baseline sin inteligencia. Elige 2 servicios al azar y les asigna réplicas aleatorias en `[min_pod, max_pod]`.
- **`NoneController.py`** — Baseline nulo. No hace nada durante la duración del experimento; sirve como piso de referencia.

---

## 8. Modelos de Simulación (`simulation/`)

Estos scripts entrenan y evalúan modelos que estiman la recompensa de SLO en función de (QPS, réplicas) por servicio. Su propósito es alimentar la función de aptitud del GA.

- **`RandomForestClassify.py`** — El modelo que realmente usa PBScaler en producción. Clasificador binario (viola/no viola SLO). Guarda el modelo en disco con `joblib`.
- **`Bagging.py`, `DecisionTree.py`, `Linear.py`, `MLP.py`, `SVM.py`** — Modelos alternativos evaluados en el estudio original (regresores y clasificadores). Comparten el mismo pipeline de carga de datos y generación de curvas ROC / métricas. No persisten modelos más allá de la evaluación.

Todos consumen trazas CSV con columnas `svc&qps`, `svc&count` y etiqueta `slo_reward`.

---

## 9. Evaluación (`evaluation/`)

- **`Draw.py`** — Suite de visualización. Genera gráficas SVG de latencia p90 vs. carga, réplicas totales vs. carga, tasa de éxito vs. carga, CDF de réplicas, y consumo de vCPU/memoria vs. carga.
- **`Evaluation.py`** — Calculadora de métricas agregadas: tiempo de respuesta promedio, porcentaje de conflictos SLA, número de eventos de escalado, costo de recursos (precios: `0.00003334/vCPU·s`, `0.00001389/GB·s`), costo de disponibilidad por tramos de éxito, y media de pods. Función `evaluation()` devuelve una 5-tupla resumen.

---

## 10. Experimentos en GKE (`scripts/`)

Conjunto de scripts que automatizan experimentos de extremo a extremo en Google Kubernetes Engine:

| Script | Rol |
|--------|-----|
| `setup_gke.sh` | Provisiona cluster GKE (3 nodos e2-standard-4), instala Istio 1.13.4 y kube-prometheus-stack, despliega Online Boutique. |
| `teardown_gke.sh` | Limpia todo: Helm releases, namespaces, Istio y el cluster. |
| `run_khpa_baseline.sh` | Experimento completo con HPA: carga de tráfico, recolección de métricas, gráficas. |
| `run_pbscaler_baseline.sh` | Experimento completo con PBScaler. Lanza el controlador, corre Locust, extrae resúmenes estructurados del log. |
| `run_pbscaler_pymoo.sh` | Variante con análisis exhaustivo del GA (evolución, ciclos, gráficas de réplicas). |
| `run_pbscaler_debug.sh` | Variante mínima para depuración. |
| `locustfile.py` | Generador de carga para Online Boutique. Rampa escalonada 10→50→100→150→200 usuarios durante 10 minutos con distribución realista de tareas (browse, view, cart, checkout, etc.). |
| `collect_metrics.py` | Extrae CSVs de Prometheus vía HTTP: latencia p95, violaciones de SLO, QPS, instancias. |
| `plot_results.py` | Gráficas por experimento (latencia, réplicas, recursos, violaciones acumuladas). |
| `plot_comparison.py` | Gráficas de comparación lado a lado KHPA vs PBScaler. |
| `generate_training_data.py` | Convierte resultados del baseline KHPA en datos de entrenamiento para el Random Forest (con aumentación sintética). |

---

## 11. Benchmarks (`benchmarks/`)

Dos aplicaciones de microservicios incluidas como bancos de prueba:

- **Online Boutique** (`microservices-demo/`) — 10 microservicios en Go, Python, etc.
- **Train-Ticket** (`train-ticket/`) — 43 microservicios en Java Spring Boot.

Despliegue: `kubectl apply -f benchmarks/<app>/kubernetes-manifests/`.

---

## 12. Rama Experimental de Aprendizaje por Refuerzo (`RL/`)

Rama paralela **no utilizada por `main.py`** que explora autoescalado mediante RL con redes neuronales gráficas (GNN). No es parte del camino principal del proyecto, pero representa trabajo experimental significativo.

### Entorno y Simulación
- **`Environment.py`** — Entorno estilo Gym que envuelve Prometheus + Kubernetes. Construye el grafo de servicios, ejecuta acciones de escalado, espera convergencia y calcula recompensa combinando cumplimiento de SLO y eficiencia de pods. **Nota:** tiene valores hardcodeados (`redis-cart`, `SLO=200`) que ignoran `config.yaml`.
- **`Simulation.py`** — Simulador sintético basado en trazas reales. Entrena un modelo de transición de estados con GNN para permitir entrenamiento offline.

### Componentes GNN comunes (`RL/common/`)
- **`GAT.py`** — Capas de convolución gráfica (GCN y GAT) basadas en `torch_geometric`.
- **`MPNN.py`** — Red de paso de mensajes personalizada (tres capas FC, batch norm, ReLU).
- **`StateModel.py`** — Predictor de métricas futuras (CPU, memoria, p90) entrenado con MSE.

### Agentes RL clásicos (`RL/film/`)
- **`D3QN.py`** — Dueling Double DQN con replay y target network.
- **`DDPG.py`** — Actor-crítico determinista con ruido Ornstein-Uhlenbeck.
- **`TD3.py`** — DDPG gemelo con crítico dual y updates retardados del actor.
- **`actorcritic.py`** — Arquitecturas compartidas de actor y crítico.
- **`noise.py`** — Generadores de ruido (gaussiano, OU, adaptativo).
- **`replaybuffer.py`** — Buffer circular FIFO basado en `deque`.

### Agentes RL con conciencia de grafos (`RL/grScaler/`)
- **`GrScaler_D3QN.py`, `GrScaler_TD3.py`, `GraScaler_DDPG.py`** — Variantes de los agentes `film/` con capas gráficas (GCN/GAT) en el actor y crítico.
- **`GrScaler_warm.py`** — Variante de arranque cálido con MPNN y entrenamiento más corto.
- **`GraphData.py`** — Utilidad para empaquetar datos de grafo en objetos `torch_geometric.data.Data`.
- **`GraphPolicyNet.py`** — Red de política gráfica autónoma (MPNN → FC → Softmax).
- **`RandomPolicy.py`** — Política aleatoria para comparación.

---

## 13. Pruebas (`tests/`)

El proyecto incluye una suite de pruebas integrales que **no requiere cluster en vivo** — todas las pruebas corren contra mocks de Prometheus y Kubernetes.

### Arquitectura de Mocks

- **`mocks/mock_prometheus.py`** — Servidor HTTP real (no stub de biblioteca) que responde a consultas PromQL. Expone tres escenarios: `normal_load`, `single_bottleneck` (checkout 350 ms, CPU 0.92) y `cascading_bottleneck` (payment 280 ms tras resolver checkout). Soporta cambio de escenario en tiempo de ejecución para pruebas dinámicas.
- **`mocks/mock_kubernetes.py`** — Cliente de Kubernetes en memoria. Implementa la misma interfaz que `KubernetesClient.py` y rastrea todas las llamadas a `patch_scale()` para permitir aserciones.
- **`mocks/scenarios.py`** — Catálogo de escenarios con constantes (`SLO_MS=200`, `ANOMALY_THRESHOLD=220`), lista de servicios y aristas de llamada, y *dataclass* `ScenarioState` con latencias, réplicas, QPS y utilización de CPU.
- **`conftest.py`** — Parchea `kubernetes` y `schedule` en `sys.modules` antes de la colección de pytest, permitiendo ejecución offline sin dependencias pesadas instaladas.

### Casos de Prueba

- **`test_pipeline.py`** — Suite principal dividida en cuatro clases:
  - `TestAnomalyDetection` — verifica `get_abnormal_calls()` en los tres escenarios.
  - `TestRootCauseAnalysis` — verifica que PageRank identifica el servicio correcto.
  - `TestGAOptimisation` — verifica que `choose_action('add')` llama a `patch_scale()` dentro de los límites (usa un `MockGA` ligero para evitar la dependencia del modelo RF).
  - `TestScenarioSwitching` — verifica el cambio dinámico de escenarios en el mock.
- **`run_single_bottleneck.py`** — Ejecutable de *dry-run* que corre el pipeline completo contra el escenario `single_bottleneck`, con logs tabulares por fase y verifica que el checkout escale de 2 a 3 réplicas.

---

## 14. Flujo de Trabajo Típico

1. **Entrenar el predictor**: ejecutar `simulation/RandomForestClassify.py` con datos de trazas para generar `simulation/boutique/RandomForestClassify.model`.
2. **Configurar**: editar `config.yaml` con los endpoints del cluster, SLO y cotas de réplicas.
3. **Desplegar benchmark**: `kubectl apply -f benchmarks/microservices-demo/kubernetes-manifests/`.
4. **Lanzar PBScaler**: `python main.py` (o un script de `scripts/run_*.sh` para un experimento completo en GKE).
5. **Analizar**: los CSVs quedan en `output/`; usar `evaluation/Draw.py` y `evaluation/Evaluation.py` o `scripts/plot_*.py` para visualizar y comparar.

---

## 15. Problemas Conocidos

- Las rutas a datos de entrenamiento están hardcodeadas en `simulation/RandomForestClassify.py`.
- No hay verificación de conectividad con Prometheus ni Kubernetes al arrancar.
- `RL/Environment.py` hardcodea la exclusión del nodo `redis-cart` y `SLO=200`, ignorando `config.yaml`.
- `requirements.txt` omite `pymoo`, `scikit-learn` y `networkx` aunque son importados en tiempo de ejecución.
- El controlador activo en `main.py` está fijo en el código; cambiar de baseline implica editar la fuente.
- `util/Spectrum.py` contiene código de localización de fallos que no parece conectarse al pipeline actual.

---

## 16. Resumen Ejecutivo

PBScaler es un sistema de **tres fases** (detección → diagnóstico → optimización) que explota la estructura del grafo de llamadas de microservicios para escalar **solo los servicios raíz** de las violaciones de SLO, en lugar de escalar todo indiscriminadamente. Su valor principal radica en:

- **Precisión del diagnóstico** mediante PageRank personalizado con potencial topológico.
- **Eficiencia de la decisión** mediante un GA cuya aptitud combina cumplimiento de SLO (modelado por Random Forest) y costo.
- **Reproducibilidad experimental** mediante scripts de GKE, generadores de carga y una suite de pruebas offline con mocks fieles.

El repositorio también alberga una línea experimental de RL con GNN que explora alternativas al GA para la fase de optimización, y una familia completa de baselines (HPA, MicroScaler, SHOWAR, Random) para comparación rigurosa.
