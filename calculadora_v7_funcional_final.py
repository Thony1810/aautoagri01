# calculadora_v9_profesional.py
# ============================================================
# MEJORAS v9 vs v8:
# - Filtro COSNXS agregado: Muestra pantes que se cosecharán con IM NAX
# - Correcciones en pestaña TIFF/Raster línea de tiempo
# - Mejora en consulta SQL de ca_pantes para identificar COSNXS
# 
# MEJORAS v8 vs v7:
# - Frontend mejorado: Layout profesional, minimalista, tipo centro de monitoreo
# - Filtro PANTE1 simplificado: Solo muestra PANTE1
# - Tabla priorización: IM ACTUAL en decimal (2 decimales, 0-1)
# - Curva IM: Precarga automática al iniciar
# - Mapa de Alertas: Clustering por zoom, panel lateral, shapefile base
# - Backend: Validación de tendencia decreciente en fórmula ganadora
# - Acceso LAN: Bind a 0.0.0.0, IP 192.168.52.62
# - Nueva pestaña TIFF: Raster por PANTE1 con paleta NAX 9 colores
# ============================================================

import os
import json
import math
import hashlib
from datetime import datetime, date
from pathlib import Path
from functools import lru_cache
from typing import Optional

import numpy as np
import pandas as pd

# SciPy no disponible en esta versión: PCHIP deshabilitado
PchipInterpolator = None
from flask import Flask, request, jsonify, Response

# GIS para TIFF y shapefile (v8)
try:
    import rasterio
    import geopandas as gpd
    from rasterio.mask import mask
    from shapely.geometry import shape
except ImportError:
    rasterio = None
    gpd = None

# ODBC (opcional) para traer BALANCE de IBM4 si no hay CSV
try:
    import pyodbc  # type: ignore
except Exception:
    pyodbc = None  # type: ignore

# -----------------------------
# CONFIG
# -----------------------------
DEFAULT_DIARIO = Path(r"C:\Users\AAUTOAGRI01\OneDrive - Compania Agricola Industrial Santa Ana, S. A\Escritorio\PROYECCION DIAS POR MADURACION\panel_diario_IM_2526.csv")
DEFAULT_RESUMEN = Path(r"C:\Users\AAUTOAGRI01\OneDrive - Compania Agricola Industrial Santa Ana, S. A\Escritorio\PROYECCION DIAS POR MADURACION\panel_resumen_IM_2526.csv")

DEFAULT_ZAFRA = "2025/2026"
DEFAULT_COD_MODELO = 7

REQ_DIARIO_COLS = [
    "PANTE1","FECHA","EDAD_DIAS","IM_OBS","IM_INTERP",
    "REGION_HIST_MODE","NOMFIN_HIST_MODE","VARIE_HIST_MODE","ESTRATO_HIST_MODE"
]
REQ_RESUMEN_COLS = [
    "PANTE1","IM_NOW","EDAD_DIAS_FIN","FECHA_PANEL_FIN","N",
    "MODEL_USED","MODEL_TYPE","R2","RMSE","MODEL_FORMULA","MODEL_PARAMS",
    "REGION_HIST_MODE","NOMFIN_HIST_MODE","ESTRATO_HIST_MODE","VARIE_HIST_MODE"
]

FILTER_COLS = ["COSNXS","REGION_HIST_MODE","NOMFIN_HIST_MODE","VARIE_HIST_MODE","ESTRATO_HIST_MODE","PANTE1"]
# Filtros adicionales del balance de cosecha
BALANCE_FILTER_COLS = ["SEMANA_PROY","TERCIO_PROY"]
RULE_MIN_EDAD = 200
GRID_N = 200

DEFAULT_BALANCE = Path(r"C:\Users\AAUTOAGRI01\OneDrive - Compania Agricola Industrial Santa Ana, S. A\Escritorio\PROYECCION DIAS POR MADURACION\balance_cosecha_2526.csv")

# Balance de cosecha: fuente preferida
# - "IBM4": consulta directamente a la BD (recomendado)
# - "CSV":  usa DEFAULT_BALANCE únicamente
BALANCE_MODE = os.getenv("BALANCE_MODE", "IBM4").strip().upper()

BALANCE_ZAFRA = os.getenv("BALANCE_ZAFRA", "2025/2026").strip()
try:
    BALANCE_COD_MODELO = int(os.getenv("BALANCE_COD_MODELO", "7").strip())
except Exception:
    BALANCE_COD_MODELO = 7

# Si BALANCE_CACHE_CSV=1, se guarda/actualiza DEFAULT_BALANCE al consultar IBM4
BALANCE_CACHE_CSV = os.getenv("BALANCE_CACHE_CSV", "1").strip() == "1"


# IBM4 / ODBC (opcional)
# Si balance_cosecha_2526.csv no existe, el sistema intentará consultarlo en IBM4.
# Configura por variables de entorno (recomendado):
#   IBM4_DSN, IBM4_USER, IBM4_PWD
# o bien un TXT (IBM4_CRED_TXT) con 2 líneas: usuario y password.
IBM4_DSN = os.environ.get("IBM4_DSN", "IBM4")
IBM4_USER = os.environ.get("IBM4_USER", "")
IBM4_PWD  = os.environ.get("IBM4_PWD", "")
IBM4_CRED_TXT = Path(os.environ.get("IBM4_CRED_TXT", r"C:\Users\AAUTOAGRI01\OneDrive - Compania Agricola Industrial Santa Ana, S. A\Escritorio\NAX_Anomalias - 25-26\credenciales.txt"))

REQ_BALANCE_COLS = ["PANTE1","FECHACOS_PROY","SEMANA_PROY","TERCIO_PROY"]
BALANCE_EXTRA_COLS = ["ZAFRA","TIPO_CORTE","TONSHA"]

# -----------------------------
# CONFIG v8 - LAN y Nueva Pestaña
# -----------------------------
HOST_IP = "192.168.52.62"
SHAPEFILE_PATH = Path(r"C:\Sahpe SAnta Ana\SANTA ANA 25 26\Extpan_Santa_Ana25-26\Extpan_Santa_Ana25-26.shp")

# Paleta NAX 9 colores para raster TIFF
NAX_COLORS_9 = [
    "#820000", "#C00000", "#FF1400",
    "#FF8C00", "#FFC000",
    "#B4FF00", "#50FF00", "#00C800", "#007800"
]
NAX_BOUNDS_9 = [0.0, 0.10, 0.20, 0.30, 0.40, 0.50, 0.60, 0.70, 0.85, 1.0]

# -----------------------------
# LOAD & NORMALIZE
# -----------------------------
# -----------------------------
# VALIDACIÓN DE TENDENCIA DECRECIENTE (v8)
# -----------------------------
def _validate_decreasing_trend(x_grid: np.ndarray, y_grid: list, threshold: float = -0.001) -> bool:
    """
    Valida que la curva tenga comportamiento realista de maduración: al aumentar edad, IM debe disminuir.
    Reglas (más estrictas para evitar curvas "aplanadas" o crecientes):
      1) Pendiente lineal global < 0 y suficientemente negativa (no casi 0)
      2) En promedio, el final debe ser menor o igual que el inicio
      3) En versión suavizada (promedio móvil), la pendiente también debe ser negativa
      4) No se permiten incrementos locales grandes (tol pequeña)
    threshold: umbral mínimo para pendiente promedio (negativo).
    """
    valid_indices = [i for i, y in enumerate(y_grid) if y is not None and isinstance(y, (int, float)) and (y == y)]
    if len(valid_indices) < 3:
        return False

    x_valid = np.asarray(x_grid[valid_indices], dtype=float)
    y_valid = np.asarray([y_grid[i] for i in valid_indices], dtype=float)

    # pendiente global (sin scipy)
    slope = float(np.polyfit(x_valid, y_valid, 1)[0])

    # Evitar curvas planas: exigir pendiente negativa mínima
    min_abs = max(0.0008, abs(float(threshold)))  # 0.0008 IM/día por defecto
    if slope >= -min_abs:
        return False

    # Umbral extra (si se pasó threshold más estricto)
    if slope >= threshold:
        return False

    # tendencia global: fin <= inicio
    if (y_valid[-1] - y_valid[0]) > 0:
        return False

    # Incrementos locales permitidos (tolerancia pequeña)
    dy = np.diff(y_valid)
    if np.max(dy) > 0.01:
        return False

    # Suavizado: pendiente negativa
    window = max(5, len(y_valid) // 12)
    if len(y_valid) > window:
        kernel = np.ones(window, dtype=float) / float(window)
        y_smooth = np.convolve(y_valid, kernel, mode="valid")
        if len(y_smooth) > 2:
            smooth_slope = float(np.polyfit(np.linspace(x_valid[0], x_valid[-1], len(y_smooth)), y_smooth, 1)[0])
            if smooth_slope >= -min_abs:
                return False

    return True


# ─────────────────────────────────────────────
# MOTOR DE FÓRMULA GANADORA
# Parsea MODEL_TYPE + MODEL_PARAMS del panel_resumen
# y construye funciones evaluate/invert para la calculadora.
# ─────────────────────────────────────────────
import ast as _ast

def _parse_params(raw) -> Optional[list]:
    """Convierte MODEL_PARAMS (string lista o JSON) a lista de floats."""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s or s.lower() in ("nan","none",""):
        return None
    try:
        # Intentar JSON/Python list  [0.5, -0.001, ...]
        parsed = _ast.literal_eval(s)
        if isinstance(parsed, (list, tuple)):
            return [float(x) for x in parsed]
    except Exception:
        pass
    # Intentar separado por comas  0.5, -0.001
    try:
        return [float(x.strip()) for x in s.split(",") if x.strip()]
    except Exception:
        return None


def build_formula_model(resumen: dict, n_grid: int = 300) -> Optional[dict]:
    """
    Si el panel_resumen tiene MODEL_TYPE + MODEL_PARAMS válidos,
    construye un modelo idéntico al PCHIP pero evaluado con la fórmula real.
    Soporta: poly1..poly5, exp_decay, log_growth, gompertz, logistic, lineal.
    Retorna None si no puede parsear (se usará PCHIP como fallback).
    """
    if not resumen:
        return None

    mtype  = str(resumen.get("MODEL_TYPE") or "").strip().lower()
    params = _parse_params(resumen.get("MODEL_PARAMS"))
    edad_fin = resumen.get("EDAD_DIAS_FIN")

    if not mtype or not params:
        return None

    # Determinar rango de edad para el grid
    try:
        x_max = float(edad_fin) + 365 if edad_fin is not None else 600.0
        x_min = max(0.0, float(edad_fin) - 400) if edad_fin is not None else 0.0
    except Exception:
        x_min, x_max = 100.0, 700.0

    xg = np.linspace(x_min, x_max, n_grid)

    def _eval(t):
        p = params
        try:
            if mtype in ("poly1","lineal","linear"):
                return p[0] + p[1]*t
            elif mtype == "poly2":
                return p[0] + p[1]*t + p[2]*t**2
            elif mtype in ("poly3","cubic"):
                return p[0] + p[1]*t + p[2]*t**2 + p[3]*t**3
            elif mtype == "poly4":
                return p[0]+p[1]*t+p[2]*t**2+p[3]*t**3+p[4]*t**4
            elif mtype == "poly5":
                return p[0]+p[1]*t+p[2]*t**2+p[3]*t**3+p[4]*t**4+p[5]*t**5
            elif mtype in ("exp_decay","exp"):
                # IM(t) = a * exp(b*t) + c  ó  a + b*exp(c*t)
                # Formato: [a, b, c]  =>  a*exp(b*t) + c
                return float(p[0]) * math.exp(float(p[1]) * t) + float(p[2])
            elif mtype in ("log_growth","log"):
                return float(p[0]) + float(p[1]) * math.log(max(t, 1e-9))
            elif mtype == "gompertz":
                # a * exp(-b * exp(-c*t))
                return float(p[0]) * math.exp(-float(p[1]) * math.exp(-float(p[2])*t))
            elif mtype in ("logistic","sigmoide"):
                # L / (1 + exp(-k*(t - t0)))
                L, k, t0 = float(p[0]), float(p[1]), float(p[2])
                return L / (1.0 + math.exp(-k*(t - t0)))
            else:
                return None
        except (OverflowError, ValueError, ZeroDivisionError, IndexError):
            return None

    yg = []
    for t in xg:
        v = _eval(t)
        yg.append(float(v) if v is not None and math.isfinite(v) else None)

    valid_y = [v for v in yg if v is not None]
    if len(valid_y) < 2:
        return None

    # Validación de tendencia decreciente (v8)
    if not _validate_decreasing_trend(xg, yg):
        return None  # Descartar modelo si no tiende a decrecer

    formula_str = resumen.get("MODEL_FORMULA") or f"{mtype}({params})"

    return {
        "MODEL_NAME": f"{mtype.upper()} (fórmula ganadora)",
        "MODEL_FORMULA": formula_str,
        "MODEL_TYPE": mtype,
        "R2": resumen.get("R2"),
        "RMSE": resumen.get("RMSE"),
        "EDAD_MIN": float(xg[0]),
        "EDAD_MAX": float(xg[-1]),
        "IM_MIN": min(valid_y),
        "IM_MAX": max(valid_y),
        "X_GRID": clean_list(xg.tolist()),
        "Y_GRID": clean_list(yg),
        "SOURCE": "formula_ganadora",
    }


def load_csv_strict(path: Path, required_cols: list) -> pd.DataFrame:
    df = pd.read_csv(path, low_memory=False, dtype={"PANTE1": str})
    missing = set(required_cols) - set(df.columns)
    if missing:
        raise KeyError(f"Faltan columnas en {path.name}: {sorted(missing)}")
    return df

def normalize_daily(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    str_cols = ["PANTE1","REGION_HIST_MODE","NOMFIN_HIST_MODE","VARIE_HIST_MODE","ESTRATO_HIST_MODE"]
    for c in str_cols:
        # Normalizar: strip, y convertir "nan"/"NaN"/None a ""
        df[c] = df[c].astype(str).str.strip()
        df[c] = df[c].replace({"nan":"", "NaN":"", "None":"", "none":""})

    df["FECHA"] = pd.to_datetime(df["FECHA"], errors="coerce")
    df["EDAD_DIAS"] = pd.to_numeric(df["EDAD_DIAS"], errors="coerce")
    df["IM_OBS"] = pd.to_numeric(df["IM_OBS"], errors="coerce")
    df["IM_INTERP"] = pd.to_numeric(df["IM_INTERP"], errors="coerce")

    # IM preferido: usa INTERP si disponible, si no usa OBS
    df["IM"] = df["IM_INTERP"].where(df["IM_INTERP"].notna(), df["IM_OBS"])

    df = df.dropna(subset=["PANTE1","FECHA","EDAD_DIAS","IM"])
    df = df[df["PANTE1"] != ""]  # eliminar PANTE1 vacíos
    df = df.sort_values(["PANTE1","FECHA","EDAD_DIAS"]).reset_index(drop=True)
    return df

def normalize_resumen(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    if "PANTE1" in df.columns:
        df["PANTE1"] = df["PANTE1"].astype(str).str.strip()
    if "FECHA_PANEL_FIN" in df.columns:
        df["FECHA_PANEL_FIN"] = pd.to_datetime(df["FECHA_PANEL_FIN"], errors="coerce")
    for c in ["N","IM_NOW","EDAD_DIAS_FIN","R2","RMSE"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df

# -----------------------------
# CREDENCIALES IBM4
# -----------------------------
def _read_ibm4_credentials() -> tuple[str, str]:
    """Lee credenciales IBM4 desde env o TXT (2 líneas: user y pwd)."""
    user = (IBM4_USER or "").strip()
    pwd  = (IBM4_PWD or "").strip()
    if user and pwd:
        return user, pwd
    try:
        if IBM4_CRED_TXT and IBM4_CRED_TXT.exists():
            lines = IBM4_CRED_TXT.read_text(encoding="utf-8").splitlines()
            user2 = (lines[0].strip() if len(lines) > 0 else "")
            pwd2  = (lines[1].strip() if len(lines) > 1 else "")
            if user2 and pwd2:
                return user2, pwd2
    except Exception:
        pass
    return "", ""

def _normalize_pante1_str(s: str) -> str:
    """
    Normaliza PANTE1 para garantizar match entre balance y panel_diario.
    La consulta SQL genera: ltrim(rtrim(to_char(finca,'0000'))) || ... || divpan
    lo que da p.ej. '1011010A' (sin espacios, sin padding extra).
    Aquí simplemente strip + upper para ser consistente con el resto del sistema.
    """
    return str(s).strip().upper()


# -----------------------------
# CONSULTA IBM4 PARA PANTES NO COSECHADOS (PANTER=0)
# -----------------------------
def fetch_pantes_no_cosechados_from_ibm4() -> set[str]:
    """
    Trae el catálogo de PANTE1 NO COSECHADOS desde ca_produccion usando PANTER:
      - PANTER = 0  => NO COSECHADOS
      - PANTER = 1  => YA COSECHADOS

    Retorna un set de strings normalizados (UPPER/strip) para filtrar PANEL_DIARIO y PANEL_RESUMEN.
    """
    if pyodbc is None:
        print("[PANTES/IBM4] pyodbc no disponible. No puedo consultar IBM4 para PANTER.")
        return set()

    user, pwd = _read_ibm4_credentials()
    if not user or not pwd:
        print("[PANTES/IBM4] Credenciales no configuradas. Define IBM4_USER/IBM4_PWD o IBM4_CRED_TXT.")
        return set()

    # Consulta solicitada (sin schema). Si falla, probamos con schema campo.
    sql_variants = [
        """
        SELECT 
            LPAD(codfin, 4, '0') || 
            LPAD(codpan, 4, '0') || 
            TRIM(divpan) AS PANTE1
        FROM 
            ca_produccion
        WHERE 
            panter = 0
        ORDER BY 
            1
        """,
        """
        SELECT 
            LPAD(codfin, 4, '0') || 
            LPAD(codpan, 4, '0') || 
            TRIM(divpan) AS PANTE1
        FROM 
            campo.ca_produccion
        WHERE 
            panter = 0
        ORDER BY 
            1
        """
    ]

    for sql in sql_variants:
        try:
            conn = pyodbc.connect(f"DSN={IBM4_DSN};UID={user};PWD={pwd}", timeout=15)
            df = pd.read_sql(sql, conn)
            conn.close()

            if df is None or df.empty or "PANTE1" not in df.columns:
                print("[PANTES/IBM4] Consulta ejecutada pero no retornó filas.")
                continue

            pset = set(
                df["PANTE1"]
                .fillna("")
                .astype(str)
                .apply(_normalize_pante1_str)
                .tolist()
            )
            pset = {p for p in pset if p}
            print(f"[PANTES/IBM4] OK: {len(pset)} pantes NO COSECHADOS (PANTER=0) traídos desde IBM4.")
            return pset

        except Exception as e:
            print(f"[PANTES/IBM4] Error consultando variante SQL: {e}")
            # intentar la siguiente variante
            continue

    return set()

# -----------------------------
# CONSULTA IBM4 PARA BALANCE
# -----------------------------
def fetch_balance_from_ibm4(zafra: str = "2025/2026", cod_modelo: int = 7) -> pd.DataFrame:
    """
    Ejecuta la consulta correcta (ROW_NUMBER sin GROUP BY) para traer 1 fila por PANTE1:
    zafra, pante1, FECHACOS_PROY, SEMANA_PROY, TERCIO_PROY, tipo_corte

    Requiere:
      - pyodbc instalado
      - DSN IBM4 accesible
      - credenciales en env (IBM4_USER/IBM4_PWD) o en IBM4_CRED_TXT
    """
    if pyodbc is None:
        print("[BALANCE/IBM4] pyodbc no disponible. No puedo consultar IBM4.")
        return pd.DataFrame(columns=REQ_BALANCE_COLS)

    user, pwd = _read_ibm4_credentials()
    if not user or not pwd:
        print("[BALANCE/IBM4] Credenciales no configuradas. Define IBM4_USER/IBM4_PWD o IBM4_CRED_TXT.")
        return pd.DataFrame(columns=REQ_BALANCE_COLS)

    sql = f"""
    SELECT
      zafra,
      pante1,
      fechacos_proy AS FECHACOS_PROY,
      semana        AS SEMANA_PROY,
      fn_tercio(fechacos_proy) AS TERCIO_PROY,
      tipo_corte
    FROM (
      SELECT
        a.zafra,
        ltrim(rtrim(to_char(a.finca, '0000'))) ||
        ltrim(rtrim(to_char(a.pante, '0000'))) ||
        ltrim(rtrim(a.divpan))                 AS pante1,
        a.fechacos_proy                        AS fechacos_proy,
        a.semana                               AS semana,
        decode(c.codfre, 2,'manual', 8,'manual', 11,'manual', 'mechanical') AS tipo_corte,
        a.sem_ejecutada,
        ROW_NUMBER() OVER (
          PARTITION BY a.zafra,
                       ltrim(rtrim(to_char(a.finca, '0000'))) ||
                       ltrim(rtrim(to_char(a.pante, '0000'))) ||
                       ltrim(rtrim(a.divpan))
          ORDER BY a.sem_ejecutada DESC,
                   a.fechacos_proy DESC
        ) AS rn
      FROM campo.ca_secuencia_cosecha a
      LEFT JOIN campo.ca_frente c
        ON a.frente = c.codfre
      WHERE a.zafra = ?
        AND a.cod_modelo = ?
    ) t
    WHERE rn = 1
    ORDER BY FECHACOS_PROY DESC
    """

    try:
        # Nota: para Oracle ODBC, UID/PWD funcionan con DSN estándar.
        conn = pyodbc.connect(f"DSN={IBM4_DSN};UID={user};PWD={pwd}", timeout=15)
        df = pd.read_sql(sql, conn, params=[zafra, cod_modelo])
        conn.close()

        if df is None or df.empty:
            print("[BALANCE/IBM4] Consulta ejecutada pero no retornó filas.")
            return pd.DataFrame(columns=REQ_BALANCE_COLS)

        # Normalización similar a load_balance
        df.columns = [c.strip().upper() for c in df.columns]
        if "PANTE1" in df.columns:
            df["PANTE1"] = df["PANTE1"].fillna("").apply(_normalize_pante1_str)
        if "FECHACOS_PROY" in df.columns:
            df["FECHACOS_PROY"] = pd.to_datetime(df["FECHACOS_PROY"], errors="coerce")
        for c in ["SEMANA_PROY", "TERCIO_PROY", "TIPO_CORTE", "ZAFRA"]:
            if c in df.columns:
                df[c] = df[c].fillna("").astype(str).str.strip()

        # Garantizar columnas mínimas
        for c in REQ_BALANCE_COLS:
            if c not in df.columns:
                df[c] = None

        out = df[[c for c in ["PANTE1","FECHACOS_PROY","SEMANA_PROY","TERCIO_PROY","ZAFRA","TIPO_CORTE"] if c in df.columns]].copy()
        out = (out.sort_values("FECHACOS_PROY", ascending=False, na_position="last")
                  .drop_duplicates(subset=["PANTE1"], keep="first")
                  .reset_index(drop=True))

        print(f"[BALANCE/IBM4] OK: {len(out)} pantes traídos desde IBM4 (zafra={zafra}, modelo={cod_modelo})")
        return out

    except Exception as e:
        import traceback
        print(f"[BALANCE/IBM4 ERROR] {e}")
        traceback.print_exc()
        return pd.DataFrame(columns=REQ_BALANCE_COLS)

# -----------------------------
# CARGA DE BALANCE DE COSECHA
# -----------------------------
def load_balance(path: Path) -> pd.DataFrame:
    """
    Balance de cosecha (logístico).

    En esta versión, la fuente por defecto es IBM4 (consulta directa) para evitar
    depender de un CSV manual que puede venir con separadores/encoding distintos.

    Control por variables de entorno:
      - BALANCE_MODE = IBM4 | CSV   (default IBM4)
      - BALANCE_ZAFRA (default 2025/2026)
      - BALANCE_COD_MODELO (default 7)
      - BALANCE_CACHE_CSV = 1|0 (default 1)  -> guarda/actualiza DEFAULT_BALANCE
    """
    # 1) Preferir IBM4 si está habilitado
    if BALANCE_MODE == "IBM4":
        df_ibm = fetch_balance_from_ibm4(zafra=BALANCE_ZAFRA, cod_modelo=BALANCE_COD_MODELO)
        if df_ibm is not None and not df_ibm.empty:
            # Normalizar a columnas mínimas esperadas
            out = df_ibm.copy()
            out.columns = [c.strip().upper() for c in out.columns]
            for c in REQ_BALANCE_COLS:
                if c not in out.columns:
                    out[c] = None

            # Asegurar tipos
            out["PANTE1"] = out["PANTE1"].fillna("").apply(_normalize_pante1_str)
            out["FECHACOS_PROY"] = pd.to_datetime(out["FECHACOS_PROY"], errors="coerce")
            if "SEMANA_PROY" in out.columns:
                out["SEMANA_PROY"] = out["SEMANA_PROY"].fillna("").astype(str).str.strip()
            if "TERCIO_PROY" in out.columns:
                out["TERCIO_PROY"] = out["TERCIO_PROY"].fillna("").astype(str).str.strip()

            # Deduplicar por PANTE1 (por si acaso)
            out = (out.sort_values("FECHACOS_PROY", ascending=False, na_position="last")
                     .drop_duplicates(subset=["PANTE1"], keep="first")
                     .reset_index(drop=True))

            # Cache opcional a CSV
            if BALANCE_CACHE_CSV:
                try:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    out.to_csv(path, index=False, encoding="utf-8")
                    print(f"[BALANCE] Cache CSV actualizado desde IBM4: {path}")
                except Exception as e:
                    print(f"[BALANCE] No pude guardar cache CSV: {e}")

            keep_cols = [c for c in (REQ_BALANCE_COLS + [x for x in BALANCE_EXTRA_COLS if x in out.columns]) if c in out.columns]
            print(f"[BALANCE] Cargado desde IBM4: {len(out)} pantes")
            return out[keep_cols].copy()

        print("[BALANCE] IBM4 no retornó datos o falló la conexión.")
        print("          Intentaré cargar CSV como respaldo (si existe).")

    # 2) Fallback: CSV (si existe)
    if not path.exists():
        print(f"[BALANCE] Archivo no encontrado: {path}")
        print("[BALANCE] Balance cosecha: NO CARGADO")
        print("          Exporta la consulta SQL a balance_cosecha_2526.csv con columnas:")
        print("          PANTE1, FECHACOS_PROY, SEMANA_PROY, TERCIO_PROY")
        return pd.DataFrame(columns=REQ_BALANCE_COLS)

    try:
        # Intentar leer con separadores comunes
        last_err = None
        df = None
        for sep in [",", ";", "\t", "|"]:
            try:
                df = pd.read_csv(path, sep=sep, dtype=str, engine="python")
                if df is not None and df.shape[1] >= 2:
                    break
            except Exception as e:
                last_err = e
                df = None

        if df is None:
            raise RuntimeError(f"No se pudo leer el CSV con separadores comunes. Ultimo error: {last_err}")

        df.columns = [c.strip().upper() for c in df.columns]

        # Validar columnas mínimas
        missing = [c for c in REQ_BALANCE_COLS if c not in df.columns]
        if missing:
            print(f"[BALANCE ERROR] CSV sin columnas requeridas: {missing}")
            return pd.DataFrame(columns=REQ_BALANCE_COLS)

        df["PANTE1"] = df["PANTE1"].fillna("").apply(_normalize_pante1_str)
        df["FECHACOS_PROY"] = pd.to_datetime(df["FECHACOS_PROY"], errors="coerce")
        df["SEMANA_PROY"] = df["SEMANA_PROY"].fillna("").astype(str).str.strip()
        df["TERCIO_PROY"] = df["TERCIO_PROY"].fillna("").astype(str).str.strip()

        # Deduplicar por PANTE1 tomando la fecha más reciente
        df = (df.sort_values("FECHACOS_PROY", ascending=False, na_position="last")
                .drop_duplicates(subset=["PANTE1"], keep="first")
                .reset_index(drop=True))

        keep = [c for c in (REQ_BALANCE_COLS + [x for x in BALANCE_EXTRA_COLS if x in df.columns]) if c in df.columns]
        print(f"[BALANCE] Cargado desde CSV: {len(df)} pantes")
        return df[keep].copy()

    except Exception as e:
        import traceback
        print(f"[BALANCE ERROR] {e}")
        traceback.print_exc()
        return pd.DataFrame(columns=REQ_BALANCE_COLS)

# -----------------------------
# WARM UP - Verificación y carga inicial de datos (v8)
# -----------------------------
def perform_warm_up():
    """
    Warm up del sistema: verifica archivos, carga datos iniciales y reporta estado.
    Se ejecuta al importar el módulo para asegurar que todo esté listo.
    """
    print("🔥 [WARM UP] Iniciando verificación del sistema...")

    # Verificar archivos requeridos
    files_status = {
        "Panel Diario": DEFAULT_DIARIO,
        "Panel Resumen": DEFAULT_RESUMEN,
        "Balance Cosecha": DEFAULT_BALANCE,
        "Shapefile": SHAPEFILE_PATH
    }

    all_files_ok = True
    for name, path in files_status.items():
        if path.exists():
            print(f"✅ [WARM UP] {name}: {path.name} encontrado")
        else:
            print(f"❌ [WARM UP] {name}: {path.name} NO encontrado")
            all_files_ok = False

    if not all_files_ok:
        print("⚠️  [WARM UP] Algunos archivos faltan. El sistema intentará generarlos automáticamente.")

    # Intentar cargar datos
    try:
        print("📊 [WARM UP] Cargando panel diario...")
        diario = normalize_daily(load_csv_strict(DEFAULT_DIARIO, REQ_DIARIO_COLS))
        print(f"✅ [WARM UP] Panel diario: {len(diario)} registros, {diario['PANTE1'].nunique()} pantes únicos")

        print("📈 [WARM UP] Cargando panel resumen...")
        resumen = normalize_resumen(load_csv_strict(DEFAULT_RESUMEN, REQ_RESUMEN_COLS))
        print(f"✅ [WARM UP] Panel resumen: {len(resumen)} filas")

        print("⚖️  [WARM UP] Cargando balance de cosecha...")
        balance = load_balance(DEFAULT_BALANCE)
        if balance.empty:
            print("⚠️  [WARM UP] Balance vacío - se generará desde IBM4 si es posible")
        else:
            print(f"✅ [WARM UP] Balance: {len(balance)} pantes con fecha de cosecha")

        print("🎯 [WARM UP] Sistema listo para operación")
        return True

    except Exception as e:
        print(f"❌ [WARM UP] Error en carga inicial: {e}")
        import traceback
        traceback.print_exc()
        return False

# Ejecutar warm up al importar
WARM_UP_SUCCESS = perform_warm_up()

PANEL_DIARIO = normalize_daily(load_csv_strict(DEFAULT_DIARIO, REQ_DIARIO_COLS))
PANEL_RESUMEN = normalize_resumen(load_csv_strict(DEFAULT_RESUMEN, REQ_RESUMEN_COLS))


# -----------------------------
# FILTRO GLOBAL: SOLO PANTE1 NO COSECHADOS (PANTER=0)
# -----------------------------
_PANTES_NO_COSECHADOS = fetch_pantes_no_cosechados_from_ibm4()
if _PANTES_NO_COSECHADOS:
    _before_d = len(PANEL_DIARIO)
    _before_r = len(PANEL_RESUMEN)
    PANEL_DIARIO = PANEL_DIARIO[PANEL_DIARIO["PANTE1"].isin(_PANTES_NO_COSECHADOS)].reset_index(drop=True)
    PANEL_RESUMEN = PANEL_RESUMEN[PANEL_RESUMEN["PANTE1"].isin(_PANTES_NO_COSECHADOS)].reset_index(drop=True)
    print(f"[PANTES] Filtro aplicado (NO COSECHADOS). Diario: {_before_d}→{len(PANEL_DIARIO)} | Resumen: {_before_r}→{len(PANEL_RESUMEN)}")
else:
    print("[PANTES] No se pudo obtener lista IBM4 de NO COSECHADOS. Se usará el panel tal cual.")

# Nota: PANEL_DIARIO será enriquecido con columna COSNXS más adelante,
# después de cargar _COSNXS_IDX desde la base de datos

# -----------------------------
# SERIALIZACIÓN SEGURA
# (definida aquí para que load_balance y _BALANCE_IDX puedan usarla)
# -----------------------------
def safe_val(v):
    """Convierte cualquier valor a algo JSON-serializable."""
    if v is None:
        return None
    if isinstance(v, float):
        if math.isnan(v) or math.isinf(v):
            return None
        return v
    if isinstance(v, (np.floating, np.integer)):
        val = float(v) if isinstance(v, np.floating) else int(v)
        if isinstance(val, float) and (math.isnan(val) or math.isinf(val)):
            return None
        return val
    if isinstance(v, (pd.Timestamp, datetime)):
        return v.strftime("%Y-%m-%d")
    if isinstance(v, (pd.NaT.__class__,)) or pd.isna(v):
        return None
    return v

def safe_date_str(v) -> Optional[str]:
    r = safe_val(v)
    return str(r) if r is not None else None

def safe_float(v) -> Optional[float]:
    r = safe_val(v)
    try:
        return float(r) if r is not None else None
    except Exception:
        return None

def clean_list(lst) -> list:
    """Limpia lista de NaN/Inf para JSON seguro."""
    return [safe_val(x) for x in lst]

# ─────────────────────────────────────────────────────────────────
# BALANCE DE COSECHA LOGÍSTICO
# ─────────────────────────────────────────────────────────────────
# Columnas mínimas requeridas en el CSV (nombres UPPERCASE):
#   PANTE1          → concatenación finca(4d)+pante(4d)+divpan  ej: "01011010A"
#   FECHACOS_PROY   → fecha programada de cosecha               ej: "2026-03-15"
#   SEMANA_PROY     → semana logística                          ej: "10"
#   TERCIO_PROY     → tercio del año                            ej: "I", "II", "III"
#
# Columnas opcionales que enriquecen el panel (si están en el CSV):
#   ZAFRA, TIPO_CORTE, TONSHA
#
# NOTA: La consulta SQL usa ROW_NUMBER() OVER(...ORDER BY MAX(sem_ejecutada) DESC,
#       MAX(fecha_cosecha) DESC) WHERE orden=1, es decir ya trae 1 fila por PANTE1.
#       Si exportas el CSV directamente de esa consulta, el CSV ya viene deduplicado.
# ─────────────────────────────────────────────────────────────────


def fetch_balance_from_ibm4(zafra: str = "2025/2026", cod_modelo: int = 7) -> pd.DataFrame:
    """
    Ejecuta la consulta correcta (ROW_NUMBER sin GROUP BY) para traer 1 fila por PANTE1:
    zafra, pante1, FECHACOS_PROY, SEMANA_PROY, TERCIO_PROY, tipo_corte

    Requiere:
      - pyodbc instalado
      - DSN IBM4 accesible
      - credenciales en env (IBM4_USER/IBM4_PWD) o en IBM4_CRED_TXT
    """
    if pyodbc is None:
        print("[BALANCE/IBM4] pyodbc no disponible. No puedo consultar IBM4.")
        return pd.DataFrame(columns=REQ_BALANCE_COLS)

    user, pwd = _read_ibm4_credentials()
    if not user or not pwd:
        print("[BALANCE/IBM4] Credenciales no configuradas. Define IBM4_USER/IBM4_PWD o IBM4_CRED_TXT.")
        return pd.DataFrame(columns=REQ_BALANCE_COLS)

    sql = f"""
    SELECT
      zafra,
      pante1,
      fechacos_proy AS FECHACOS_PROY,
      semana        AS SEMANA_PROY,
      fn_tercio(fechacos_proy) AS TERCIO_PROY,
      tipo_corte
    FROM (
      SELECT
        a.zafra,
        ltrim(rtrim(to_char(a.finca, '0000'))) ||
        ltrim(rtrim(to_char(a.pante, '0000'))) ||
        ltrim(rtrim(a.divpan))                 AS pante1,
        a.fechacos_proy                        AS fechacos_proy,
        a.semana                               AS semana,
        decode(c.codfre, 2,'manual', 8,'manual', 11,'manual', 'mechanical') AS tipo_corte,
        a.sem_ejecutada,
        ROW_NUMBER() OVER (
          PARTITION BY a.zafra,
                       ltrim(rtrim(to_char(a.finca, '0000'))) ||
                       ltrim(rtrim(to_char(a.pante, '0000'))) ||
                       ltrim(rtrim(a.divpan))
          ORDER BY a.sem_ejecutada DESC,
                   a.fechacos_proy DESC
        ) AS rn
      FROM campo.ca_secuencia_cosecha a
      LEFT JOIN campo.ca_frente c
        ON a.frente = c.codfre
      WHERE a.zafra = ?
        AND a.cod_modelo = ?
    ) t
    WHERE rn = 1
    ORDER BY FECHACOS_PROY DESC
    """

    try:
        # Nota: para Oracle ODBC, UID/PWD funcionan con DSN estándar.
        conn = pyodbc.connect(f"DSN={IBM4_DSN};UID={user};PWD={pwd}", timeout=15)
        df = pd.read_sql(sql, conn, params=[zafra, cod_modelo])
        conn.close()

        if df is None or df.empty:
            print("[BALANCE/IBM4] Consulta ejecutada pero no retornó filas.")
            return pd.DataFrame(columns=REQ_BALANCE_COLS)

        # Normalización similar a load_balance
        df.columns = [c.strip().upper() for c in df.columns]
        if "PANTE1" in df.columns:
            df["PANTE1"] = df["PANTE1"].fillna("").apply(_normalize_pante1_str)
        if "FECHACOS_PROY" in df.columns:
            df["FECHACOS_PROY"] = pd.to_datetime(df["FECHACOS_PROY"], errors="coerce")
        for c in ["SEMANA_PROY", "TERCIO_PROY", "TIPO_CORTE", "ZAFRA"]:
            if c in df.columns:
                df[c] = df[c].fillna("").astype(str).str.strip()

        # Garantizar columnas mínimas
        for c in REQ_BALANCE_COLS:
            if c not in df.columns:
                df[c] = None

        out = df[[c for c in ["PANTE1","FECHACOS_PROY","SEMANA_PROY","TERCIO_PROY","ZAFRA","TIPO_CORTE"] if c in df.columns]].copy()
        out = (out.sort_values("FECHACOS_PROY", ascending=False, na_position="last")
                  .drop_duplicates(subset=["PANTE1"], keep="first")
                  .reset_index(drop=True))

        print(f"[BALANCE/IBM4] OK: {len(out)} pantes traídos desde IBM4 (zafra={zafra}, modelo={cod_modelo})")
        return out

    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"[BALANCE/IBM4] Error: {e}")
        return pd.DataFrame(columns=REQ_BALANCE_COLS)


def fetch_cosnxs_pantes_from_ibm4(zafra: str = "2025/2026") -> pd.DataFrame:
    """
    Obtiene la lista de PANTES que se cosecharán con IM NAX (cosnxs != null)
    desde la tabla ca_pantes.
    
    Consulta SQL:
    SELECT cosnxs,
        ltrim(rtrim(to_char(codfin, '0000'))) ||
        ltrim(rtrim(to_char(codpan, '0000'))) ||
        ltrim(rtrim(divpan)) AS pante1
    FROM ca_pantes
    WHERE zafra = '2025/2026'
    
    Retorna DataFrame con columnas: PANTE1, COSNXS
    """
    if pyodbc is None:
        print("[COSNXS/IBM4] pyodbc no disponible. No puedo consultar IBM4.")
        return pd.DataFrame(columns=["PANTE1", "COSNXS"])

    user, pwd = _read_ibm4_credentials()
    if not user or not pwd:
        print("[COSNXS/IBM4] Credenciales no configuradas.")
        return pd.DataFrame(columns=["PANTE1", "COSNXS"])

    sql = """
    SELECT 
      cosnxs,
      ltrim(rtrim(to_char(codfin, '0000'))) ||
      ltrim(rtrim(to_char(codpan, '0000'))) ||
      ltrim(rtrim(divpan)) AS pante1
    FROM campo.ca_pantes
    WHERE zafra = ?
      AND cosnxs IS NOT NULL
    ORDER BY pante1
    """

    try:
        conn = pyodbc.connect(f"DSN={IBM4_DSN};UID={user};PWD={pwd}", timeout=15)
        df = pd.read_sql(sql, conn, params=[zafra])
        conn.close()

        if df is None or df.empty:
            print(f"[COSNXS/IBM4] No hay pantes con COSNXS para zafra {zafra}")
            return pd.DataFrame(columns=["PANTE1", "COSNXS"])

        # Normalizar columnas
        df.columns = [c.strip().upper() for c in df.columns]
        if "PANTE1" in df.columns:
            df["PANTE1"] = df["PANTE1"].fillna("").apply(_normalize_pante1_str)
        if "COSNXS" in df.columns:
            df["COSNXS"] = df["COSNXS"].fillna("").astype(str).str.strip()

        print(f"[COSNXS/IBM4] OK: {len(df)} pantes con COSNXS traídos desde IBM4")
        return df[["PANTE1", "COSNXS"]].copy()

    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"[COSNXS/IBM4] Error: {e}")
        return pd.DataFrame(columns=["PANTE1", "COSNXS"])


# Crear índice de balance para lookups rápidos por PANTE1
PANEL_BALANCE = load_balance(DEFAULT_BALANCE)
_BALANCE_IDX = {
    row["PANTE1"]: {k: safe_val(row.get(k)) for k in (REQ_BALANCE_COLS + BALANCE_EXTRA_COLS)}
    for _, row in PANEL_BALANCE.iterrows()
    if row.get("PANTE1")
} if not PANEL_BALANCE.empty else {}

# Cargar PANTES con COSNXS (cosecha con IM NAX)
try:
    PANEL_COSNXS = fetch_cosnxs_pantes_from_ibm4(DEFAULT_ZAFRA)
    _COSNXS_IDX = {
        row["PANTE1"]: row["COSNXS"]
        for _, row in PANEL_COSNXS.iterrows()
        if row.get("PANTE1")
    } if not PANEL_COSNXS.empty else {}
    print(f"[COSNXS] Cargados {len(_COSNXS_IDX)} pantes con indicador NAX")
except Exception as e:
    print(f"[COSNXS] Error al cargar datos: {e}")
    PANEL_COSNXS = pd.DataFrame(columns=["PANTE1", "COSNXS"])
    _COSNXS_IDX = {}

# Enriquecer PANEL_DIARIO con columna COSNXS (ahora que _COSNXS_IDX está definido)
# Esto permite filtrar por pantes que se cosecharán con IM NAX
if _COSNXS_IDX and not PANEL_DIARIO.empty:
    PANEL_DIARIO["COSNXS"] = PANEL_DIARIO["PANTE1"].map(lambda x: _COSNXS_IDX.get(x, ""))
    print(f"[PANEL] Columna COSNXS agregada a panel diario: {PANEL_DIARIO['COSNXS'].notna().sum()} pantes con NAX")
else:
    PANEL_DIARIO["COSNXS"] = ""
    print(f"[PANEL] Columna COSNXS inicializada vacía (sin datos de NAX)")


def get_balance_entry(pante1: str) -> dict:
    """Devuelve siempre un dict (sin None/undefined) para el PANTE1."""
    p = _normalize_pante1_str(pante1 or "")
    base = {
        "FECHACOS_PROY": None,
        "SEMANA_PROY": "",
        "TERCIO_PROY": "",
        "ZAFRA": DEFAULT_ZAFRA,
        "TIPO_CORTE": ""
    }
    if not p:
        return base
    ent = get_balance_row(p)
    if not ent:
        return base
    # Merge asegurando llaves
    out = base.copy()
    out.update({k: ent.get(k, out.get(k)) for k in out.keys()})
    return out

def get_balance_row(pante1: str) -> dict:
    """
    Retorna datos logísticos del pante.
    Intenta normalización exacta primero; si falla, busca por substring
    para tolerar diferencias de padding.
    """
    key = _normalize_pante1_str(pante1)
    if key in _BALANCE_IDX:
        return dict(_BALANCE_IDX[key])
    # Fallback: buscar si alguna clave contiene o está contenida en key
    for k, v in _BALANCE_IDX.items():
        if k.endswith(key) or key.endswith(k):
            return dict(v)
    return {}

def get_balance_options() -> dict:
    """Retorna listas únicas de SEMANA_PROY y TERCIO_PROY para filtros."""
    if PANEL_BALANCE.empty:
        return {"SEMANA_PROY": [], "TERCIO_PROY": []}

    def _clean_vals(col):
        return sorted(set(
            PANEL_BALANCE[col]
            .dropna().astype(str).str.strip()
            .pipe(lambda s: s[s.str.len() > 0])
            .pipe(lambda s: s[~s.isin(["nan","NaN","None","null"])])
            .unique().tolist()
        ), key=lambda x: (len(x), x))

    return {
        "SEMANA_PROY": _clean_vals("SEMANA_PROY"),
        "TERCIO_PROY": _clean_vals("TERCIO_PROY"),
    }

def get_pante1_label(pante1: str) -> str:
    """
    Genera etiqueta enriquecida para el dropdown de PANTE1:
    'PANTE1 | Sem.X | Tercio Y | DD/MM/YYYY'
    Si no hay datos de balance, retorna solo PANTE1.
    """
    bal = get_balance_row(pante1)
    if not bal:
        return pante1
    parts = [pante1]
    if bal.get("SEMANA_PROY"):
        parts.append(f"Sem.{bal['SEMANA_PROY']}")
    if bal.get("TERCIO_PROY"):
        parts.append(f"T{bal['TERCIO_PROY']}")
    fc = bal.get("FECHACOS_PROY")
    if fc:
        try:
            from datetime import datetime as _dt
            d = _dt.strptime(str(fc), "%Y-%m-%d")
            parts.append(d.strftime("%d/%m/%y"))
        except Exception:
            parts.append(str(fc))
    return " | ".join(parts)

# Pantes activos (no cosechados) = los que tienen registro en panel_resumen con modelo válido
# Si panel_resumen tiene una columna COSECHA_FLAG la usamos; si no, asumimos que todos
# los que están en panel_resumen son activos/pendientes de cosecha.
_PANTES_ACTIVOS = set(
    PANEL_RESUMEN["PANTE1"]
    .dropna()
    .astype(str)
    .str.strip()
    .pipe(lambda s: s[s.str.len() > 0])
    .unique()
    .tolist()
)
# Si el panel_resumen está vacío, no filtrar (mostrar todos)
if not _PANTES_ACTIVOS:
    _PANTES_ACTIVOS = set(PANEL_DIARIO["PANTE1"].dropna().astype(str).unique())

# -----------------------------
# FILTRADO
# -----------------------------
def filter_df(df: pd.DataFrame, filters: dict,
              date_ini: Optional[str] = None,
              date_fin: Optional[str] = None) -> pd.DataFrame:
    out = df.copy()
    for c in FILTER_COLS:
        v = str(filters.get(c, "") or "").strip()
        if not v or v.lower() in ("nan","none",""):
            continue
        out = out[out[c].astype(str) == v]

    # Filtros por balance de cosecha (SEMANA_PROY / TERCIO_PROY)
    # Necesitan join con PANEL_BALANCE para filtrar pantes
    for bc in BALANCE_FILTER_COLS:
        v = str(filters.get(bc, "") or "").strip()
        if not v or v.lower() in ("nan","none","") or PANEL_BALANCE.empty:
            continue
        pantes_ok = set(
            PANEL_BALANCE[PANEL_BALANCE[bc].astype(str).str.strip() == v]["PANTE1"]
            .astype(str).str.strip().tolist()
        )
        out = out[out["PANTE1"].astype(str).isin(pantes_ok)]

    if date_ini:
        di = pd.to_datetime(date_ini, errors="coerce")
        if pd.notna(di):
            out = out[out["FECHA"] >= di]
    if date_fin:
        df2 = pd.to_datetime(date_fin, errors="coerce")
        if pd.notna(df2):
            out = out[out["FECHA"] <= df2]

    return out.reset_index(drop=True)

def apply_visual_rule(sample: pd.DataFrame) -> pd.DataFrame:
    """Si edad_max>200 y la edad mínima al inicio <200, recortar a >=200."""
    if sample.empty:
        return sample
    edad_max = sample["EDAD_DIAS"].max()
    fecha_min = sample["FECHA"].min()
    if pd.isna(fecha_min):
        return sample
    edad_min_t0 = sample.loc[sample["FECHA"] == fecha_min, "EDAD_DIAS"].min()
    if (edad_max > RULE_MIN_EDAD) and (edad_min_t0 < RULE_MIN_EDAD):
        return sample[sample["EDAD_DIAS"] >= RULE_MIN_EDAD].copy().reset_index(drop=True)
    return sample

# -----------------------------
# CÁLCULOS
# -----------------------------
def compute_im_actual(sample: pd.DataFrame):
    """IM actual = media en la fecha más reciente del subset."""
    if sample.empty:
        return None, None
    sample = sample.sort_values(["FECHA","EDAD_DIAS"])
    last_date = sample["FECHA"].max()
    if pd.isna(last_date):
        row = sample.iloc[-1]
        return safe_float(row["IM"]), None
    sub = sample[sample["FECHA"] == last_date]
    im_vals = sub["IM"].dropna().values
    im_now = float(np.nanmedian(im_vals)) if len(im_vals) else None  # mediana más robusta
    return im_now, last_date

def compute_edad_actual(sample: pd.DataFrame) -> Optional[float]:
    """Edad en la fecha más reciente (mediana de todas las parcelas)."""
    if sample.empty:
        return None
    last_date = sample["FECHA"].max()
    if pd.isna(last_date):
        return None
    sub = sample[sample["FECHA"] == last_date]
    edades = sub["EDAD_DIAS"].dropna().values
    return float(np.median(edades)) if len(edades) else None

def compute_daily_im_rate(sample: pd.DataFrame, window: int = 7) -> Optional[float]:
    """
    Tasa de cambio promedio de IM por día.
    Usa las últimas `window` fechas únicas del subset.
    Retorna dIM/dt en unidades IM/día (puede ser negativo).
    """
    if sample.empty:
        return None
    agg = (
        sample[["FECHA","IM"]]
        .dropna()
        .groupby("FECHA")["IM"].median()
        .sort_index()
        .reset_index()
    )
    if len(agg) < 2:
        return None
    tail = agg.tail(window)
    if len(tail) < 2:
        return None
    dates = pd.to_datetime(tail["FECHA"])
    ims   = tail["IM"].values.astype(float)
    days  = (dates - dates.iloc[0]).dt.days.values.astype(float)
    if days[-1] == 0:
        return None
    # Regresión lineal simple dIM/dt
    denom = float(np.sum((days - days.mean())**2))
    if denom == 0:
        return None
    slope = float(np.sum((days - days.mean()) * (ims - ims.mean())) / denom)
    return round(slope, 6)

def fit_model_pchip(sample: pd.DataFrame, n_grid: int = GRID_N):
    """Ajuste PCHIP (deshabilitado en esta versión sin SciPy)."""
    if PchipInterpolator is None:
        return None
    if len(sample) < 4:
        return None

    # Agrupar por edad y usar mediana (más robusto que mean)
    dfu = (
        sample[["EDAD_DIAS","IM"]]
        .dropna()
        .groupby("EDAD_DIAS", as_index=False)["IM"].median()
        .sort_values("EDAD_DIAS")
    )
    x = dfu["EDAD_DIAS"].values.astype(float)
    y = dfu["IM"].values.astype(float)

    mask = np.isfinite(x) & np.isfinite(y)
    x, y = x[mask], y[mask]

    if len(x) < 2:
        return None
    xmin, xmax = float(x.min()), float(x.max())
    if xmin == xmax:
        return None

    xg = np.linspace(xmin, xmax, n_grid)
    p = PchipInterpolator(x, y, extrapolate=True)
    yg = p(xg)

    # ── Preferir tendencia decreciente SIEMPRE (v9.1)
    # Si el PCHIP no tiende a decrecer, forzar curva no-creciente con acumulado mínimo.
    # Esto asegura que la gráfica y la inversión IM→días sigan la lógica 1→0.
    if not _validate_decreasing_trend(xg, yg.tolist() if hasattr(yg, "tolist") else list(yg)):
        yg = np.minimum.accumulate(yg)
    # Para métricas, proyectar yhat desde el grid ya ajustado
    yhat = np.interp(x, xg, yg)

    ss_res = float(np.sum((y - yhat) ** 2))
    ss_tot = float(np.sum((y - np.mean(y)) ** 2))
    r2 = float(1 - ss_res / ss_tot) if ss_tot > 0 else float("nan")
    rmse = float(math.sqrt(float(np.mean((y - yhat) ** 2))))

    return {
        "MODEL_NAME": "PCHIP (decreciente)",
        "MODEL_FORMULA": "IM = PchipInterpolator(EDAD_DIAS) — Hermite cúbico por tramos, monotónico",
        "R2": safe_float(r2),
        "RMSE": safe_float(rmse),
        "EDAD_MIN": xmin,
        "EDAD_MAX": xmax,
        "IM_MIN": float(np.min(y)),
        "IM_MAX": float(np.max(y)),
        "X_GRID": clean_list(xg.tolist()),
        "Y_GRID": clean_list(np.array(yg, dtype=float).tolist()),
    }


def fit_model_poly_best(sample: pd.DataFrame, max_deg: int = 5, n_grid: int = GRID_N):
    """Ajusta la mejor polinómica (grado 1..max_deg) y retorna un modelo basado en GRID.

    Reglas:
    - Máximo grado 3 (por requerimiento).
    - Selección por mayor R2 y luego menor RMSE.
    - Preferencia fuerte por tendencia decreciente. Si una polinómica no cumple tendencia,
      se descarta; si ninguna cumple, se toma la mejor y se fuerza a no-creciente en el GRID.
    """
    if sample is None or len(sample) < 4:
        return None

    dfu = (
        sample[["EDAD_DIAS", "IM"]]
        .dropna()
        .groupby("EDAD_DIAS", as_index=False)["IM"].median()
        .sort_values("EDAD_DIAS")
    )
    x = dfu["EDAD_DIAS"].values.astype(float)
    y = dfu["IM"].values.astype(float)
    m = np.isfinite(x) & np.isfinite(y)
    x, y = x[m], y[m]
    if len(x) < 3:
        return None

    xmin, xmax = float(np.min(x)), float(np.max(x))
    if xmin == xmax:
        return None
    xg = np.linspace(xmin, xmax, n_grid)

    best = None
    best_any = None  # por si ninguna cumple tendencia

    def _score(r2v, rmsev):
        r2v = -1e9 if r2v is None or (isinstance(r2v, float) and not math.isfinite(r2v)) else float(r2v)
        rmsev = 1e9 if rmsev is None or (isinstance(rmsev, float) and not math.isfinite(rmsev)) else float(rmsev)
        return (r2v, -rmsev)

    for deg in range(1, max(1, int(max_deg)) + 1):
        try:
            coef_hi = np.polyfit(x, y, deg)  # [a_deg, ..., a0]
            yg = np.polyval(coef_hi, xg)
            yhat = np.polyval(coef_hi, x)

            ss_res = float(np.sum((y - yhat) ** 2))
            ss_tot = float(np.sum((y - np.mean(y)) ** 2))
            r2 = float(1 - ss_res / ss_tot) if ss_tot > 0 else float("nan")
            rmse = float(math.sqrt(float(np.mean((y - yhat) ** 2))))

            cand_any = {"deg": deg, "coef_hi": coef_hi, "r2": r2, "rmse": rmse, "yg": yg}
            if best_any is None or _score(r2, rmse) > _score(best_any["r2"], best_any["rmse"]):
                best_any = cand_any

            if _validate_decreasing_trend(xg, yg.tolist() if hasattr(yg, "tolist") else list(yg)):
                if best is None or _score(r2, rmse) > _score(best["r2"], best["rmse"]):
                    best = cand_any
        except Exception:
            continue

    chosen = best if best is not None else best_any
    if chosen is None:
        return None

    deg = int(chosen["deg"])
    coef_hi = np.array(chosen["coef_hi"], dtype=float)
    yg = np.array(chosen["yg"], dtype=float)

    if not _validate_decreasing_trend(xg, yg.tolist()):
        yg = np.minimum.accumulate(yg)

    # params en orden ascendente de potencia: [a0, a1, a2, a3]
    coef_lo = coef_hi[::-1].tolist()
    while len(coef_lo) < 4:
        coef_lo.append(0.0)
    coef_lo = coef_lo[:4]

    return {
        "MODEL_NAME": f"POLY{deg} (máx 3)",
        "MODEL_FORMULA": f"IM = a0 + a1*x + a2*x^2 + a3*x^3 (grado {deg})",
        "MODEL_TYPE": f"poly{deg}",
        "MODEL_PARAMS": clean_list([safe_float(v) for v in coef_lo]),
        "R2": safe_float(chosen.get("r2")),
        "RMSE": safe_float(chosen.get("rmse")),
        "EDAD_MIN": xmin,
        "EDAD_MAX": xmax,
        "IM_MIN": float(np.nanmin(yg)),
        "IM_MAX": float(np.nanmax(yg)),
        "X_GRID": clean_list(xg.tolist()),
        "Y_GRID": clean_list(yg.tolist()),
        "SOURCE": "formula_ganadora",
    }

def _build_extended_grid(xg_np, yg_np, max_extrap=730):
    """Extiende el grid con extrapolacion LINEAL (pendiente de borde) para evitar
    oscilaciones del spline fuera del rango de datos."""
    xmin, xmax = float(xg_np[0]), float(xg_np[-1])
    dx = (xmax - xmin) / max(len(xg_np) - 1, 1)
    n_slope = min(10, len(xg_np) - 1)
    # Pendiente borde derecho e izquierdo
    slope_r = float((yg_np[-1] - yg_np[-1 - n_slope]) / (xg_np[-1] - xg_np[-1 - n_slope])) if n_slope > 0 else 0.0
    slope_l = float((yg_np[n_slope] - yg_np[0]) / (xg_np[n_slope] - xg_np[0])) if n_slope > 0 else 0.0
    n_ext = max(int(max_extrap / max(dx, 1)) + 1, 100)
    x_r = xmax + dx * np.arange(1, n_ext + 1)
    y_r = yg_np[-1] + slope_r * (x_r - xmax)
    x_l = np.maximum(0, xmin - dx * np.arange(n_ext, 0, -1))
    y_l = yg_np[0] + slope_l * (x_l - xmin)
    return np.concatenate([x_l, xg_np, x_r]), np.concatenate([y_l, yg_np, y_r])


def invert_for_age(xg: list, yg: list, target_im: float,
                   current_age: Optional[float] = None) -> Optional[float]:
    """
    Encuentra el cruce de la curva con target_im.
    - Dentro del rango: busca sobre el grid PCHIP exacto.
    - Fuera del rango: extiende linealmente (pendiente del borde) hasta 2 anos.
    - Si hay varios cruces, retorna el mas cercano y futuro a current_age.
    """
    xg_np = np.array(xg, dtype=float)
    yg_np = np.array(yg, dtype=float)
    valid = np.isfinite(xg_np) & np.isfinite(yg_np)
    xg_np, yg_np = xg_np[valid], yg_np[valid]
    if len(xg_np) < 2:
        return None

    im_min_c = float(yg_np.min())
    im_max_c = float(yg_np.max())

    if im_min_c <= target_im <= im_max_c:
        x_search, y_search = xg_np, yg_np
    else:
        x_search, y_search = _build_extended_grid(xg_np, yg_np, max_extrap=730)

    sols = []
    for i in range(len(x_search) - 1):
        y1 = float(y_search[i]) - target_im
        y2 = float(y_search[i + 1]) - target_im
        if not (math.isfinite(y1) and math.isfinite(y2)):
            continue
        if abs(y1) < 1e-10:
            sols.append(float(x_search[i]))
            continue
        if y1 * y2 < 0:
            t = abs(y1) / (abs(y1) + abs(y2))
            sols.append(float(x_search[i]) * (1 - t) + float(x_search[i + 1]) * t)

    if not sols:
        return None

    if current_age is not None and math.isfinite(current_age):
        sols_future = [s for s in sols if s >= current_age - 1]
        if sols_future:
            return min(sols_future)
        return min(sols, key=lambda s: abs(s - current_age))

    return sorted(sols)[0]

def predict_im_at_age(xg: list, yg: list, age: float) -> Optional[float]:
    """Predice IM dado age. Extrapola linealmente fuera del rango del grid."""
    if not xg or not yg:
        return None
    if age <= xg[0]:
        # Extrapolacion lineal por la izquierda
        if len(xg) >= 2 and xg[1] != xg[0]:
            slope = (yg[1] - yg[0]) / (xg[1] - xg[0])
            return float(yg[0] + slope * (age - xg[0]))
        return float(yg[0])
    if age >= xg[-1]:
        # Extrapolacion lineal por la derecha
        if len(xg) >= 2 and xg[-1] != xg[-2]:
            slope = (yg[-1] - yg[-2]) / (xg[-1] - xg[-2])
            return float(yg[-1] + slope * (age - xg[-1]))
        return float(yg[-1])
    for i in range(len(xg) - 1):
        if xg[i] <= age <= xg[i + 1]:
            t = (age - xg[i]) / (xg[i + 1] - xg[i])
            return float(yg[i] * (1 - t) + yg[i + 1] * t)
    return None

# -----------------------------
# RESUMEN
# -----------------------------
def get_resumen_row(pante1: str) -> dict:
    if not pante1 or str(pante1).strip() in ("", "nan"):
        return {}
    rr = PANEL_RESUMEN[PANEL_RESUMEN["PANTE1"] == str(pante1).strip()]
    if rr.empty:
        return {}
    row = rr.iloc[0].to_dict()
    return {k: safe_val(row.get(k)) for k in REQ_RESUMEN_COLS if k in row}

# -----------------------------
# OPCIONES EN CASCADA
# -----------------------------
def cascaded_options(df: pd.DataFrame, filters: dict) -> dict:
    """
    Cascada real: cada nivel se filtra con los niveles previos seleccionados.
    Maneja correctamente orden y valores vacíos.
    """
    opts = {}
    accumulated = {}

    for col in FILTER_COLS:
        sub = filter_df(df, accumulated)
        # Obtener valores únicos no vacíos
        vals = (
            sub[col]
            .dropna()
            .astype(str)
            .str.strip()
            .replace({"nan":"","NaN":"","None":"","none":""})
            .pipe(lambda s: s[s != ""])
            .unique()
            .tolist()
        )
        vals = sorted(set(vals))
        # Para PANTE1: filtrar solo activos (presentes en panel_resumen = no cosechados)
        if col == "PANTE1":
            vals = [v for v in vals if v in _PANTES_ACTIVOS]
            # Guardar versión enriquecida con datos del balance para el dropdown
            opts["PANTE1_LABELS"] = {v: get_pante1_label(v) for v in vals}
        opts[col] = vals

        # Solo acumular si el valor del filtro es válido en las opciones actuales
        user_v = str(filters.get(col, "") or "").strip()
        if user_v and user_v in vals:
            accumulated[col] = user_v

    return opts

# -----------------------------
# PAYLOAD PRINCIPAL
# -----------------------------
def pack_payload(sample: pd.DataFrame, filters: dict) -> dict:
    N = int(len(sample))
    im_now, fecha_now = compute_im_actual(sample)
    edad_actual = compute_edad_actual(sample)
    daily_rate  = compute_daily_im_rate(sample)
    pante1_sel = filters.get("PANTE1", "")
    resumen_row = get_resumen_row(pante1_sel)

    # ── Requerimiento: si hay PANTE1, NO usar PCHIP; usar polinómica máx grado 3.
    if str(pante1_sel or "").strip():
        model = fit_model_poly_best(sample, max_deg=5)
        model_pchip_for_plot = None
    else:
        # Sin PANTE1: puede usar fórmula histórica (si existiera) o PCHIP como fallback
        formula_model = build_formula_model(resumen_row) if resumen_row else None
        pchip_model = None
        model = formula_model if formula_model is not None else None
        model_pchip_for_plot = None

    # Stats rápidas del subset
    stats = {}
    if N > 0:
        stats["EDAD_MIN"] = safe_float(sample["EDAD_DIAS"].min())
        stats["EDAD_MAX"] = safe_float(sample["EDAD_DIAS"].max())
        stats["IM_MEAN"] = safe_float(sample["IM"].mean())
        stats["IM_STD"] = safe_float(sample["IM"].std())
        stats["N_PANTES"] = int(sample["PANTE1"].nunique())

    balance_row = get_balance_row(pante1_sel) if pante1_sel else {}

    return {
        "filters_applied": filters,
        "N": N,
        "IM_ACTUAL": safe_float(im_now),
        "EDAD_ACTUAL": safe_float(edad_actual),
        "FECHA_BASE": safe_date_str(fecha_now),
        "MODEL": model,
        "MODEL_PCHIP": model_pchip_for_plot,
        "STATS": stats,
        "X_DATA": clean_list(sample["EDAD_DIAS"].tolist()) if N else [],
        "Y_OBS": clean_list(sample["IM_OBS"].tolist()) if N else [],
        "Y_INTERP": clean_list(sample["IM_INTERP"].tolist()) if N else [],
        "RESUMEN": resumen_row,
        "BALANCE": balance_row,
        "DAILY_RATE": safe_float(daily_rate),  # dIM/dt promedio últimos 7 días (IM/día)
    }

# -----------------------------
# CACHING — Key encoding seguro
# -----------------------------
def _filters_key(filters: dict, date_ini: str, date_fin: str) -> str:
    """Genera clave de caché usando hash para evitar colisiones con '=' en valores."""
    data = {
        "filters": {c: str(filters.get(c, "") or "") for c in FILTER_COLS},
        "balance_filters": {c: str(filters.get(c, "") or "") for c in BALANCE_FILTER_COLS},
        "date_ini": date_ini or "",
        "date_fin": date_fin or "",
    }
    raw = json.dumps(data, sort_keys=True, ensure_ascii=True)
    return hashlib.md5(raw.encode()).hexdigest()

# Guardamos el contexto para poder reconstruirlo desde la caché
_CACHE_CONTEXT = {}

@lru_cache(maxsize=512)
def _cached_compute(key: str) -> str:
    """Retorna JSON string (lru_cache solo acepta hashables)."""
    ctx = _CACHE_CONTEXT.get(key, {})
    filters = ctx.get("filters", {})
    date_ini = ctx.get("date_ini", "") or None
    date_fin = ctx.get("date_fin", "") or None

    sample = filter_df(PANEL_DIARIO, filters, date_ini, date_fin)
    sample = apply_visual_rule(sample)
    payload = pack_payload(sample, filters)
    return json.dumps(payload, ensure_ascii=False, default=str)

def cached_apply(filters: dict, date_ini: str = "", date_fin: str = "") -> dict:
    key = _filters_key(filters, date_ini, date_fin)
    _CACHE_CONTEXT[key] = {
        "filters": {c: str(filters.get(c, "") or "") for c in FILTER_COLS},
        "date_ini": date_ini or "",
        "date_fin": date_fin or "",
    }
    return json.loads(_cached_compute(key))


# ═══════════════════════════════════════════════════════════════════
# PRIORIZACIÓN Y ALERTAS — Motor de decisión logística
# ═══════════════════════════════════════════════════════════════════


# Umbrales de IM para semáforos (según aclaración de negocio)
# IM MAYOR A 0.45 = HÚMEDO / INMADURO / MUY TEMPRANO
# IM ENTRE 0.40 A 0.45 = PRÓXIMO A COSECHAR
# IM ENTRE 0.20 A 0.40 = ÓPTIMO
# IM MENOR A 0.20 = SECO / SOBREMADURO
IM_TEMPRANO      = 0.45
IM_PROXIMO_MIN   = 0.40
IM_OPTIMO_MIN    = 0.20

# Días de anticipación para alerta de cosecha próxima
ALERTA_DIAS_PROXIMOS = 14


def _clasif_im(im: Optional[float]) -> dict:
    """Clasificación de IM (solo por valor)."""
    if im is None:
        return {"color": "gray", "codigo": "SIN_DATOS", "label": "Sin datos"}
    if im < IM_OPTIMO_MIN:
        return {"color": "purple", "codigo": "SECO_SOBREMADURO", "label": "🟣 Seco / sobremaduro"}
    if im < IM_PROXIMO_MIN:
        return {"color": "green", "codigo": "OPTIMO", "label": "🟢 Óptimo"}
    if im <= IM_TEMPRANO:
        return {"color": "orange", "codigo": "PROXIMO", "label": "🟠 Próximo a cosechar"}
    return {"color": "blue", "codigo": "HUMEDO_TEMPRANO", "label": "🔵 Húmedo / inmaduro (temprano)"}


def _semaforo(im: Optional[float], dias_a_cos: Optional[int]) -> dict:
    """
    Semáforo de urgencia para logística.
    Combina IM actual con proximidad de FECHACOS_PROY.
    """
    base = _clasif_im(im)

    # Si no hay fecha programada, devolver clasificación por IM
    if dias_a_cos is None:
        return base

    # ── Casos vencidos (fecha ya pasó)
    if dias_a_cos <= 0:
        if im is None:
            return {"color": "gray", "codigo": "VENCIDA_SIN_DATOS", "label": "⚠ Cosecha vencida — sin IM"}
        if im > IM_TEMPRANO:
            return {"color": "red", "codigo": "VENCIDA_TEMPRANO", "label": "⛔ Vencida — IM alto (aún inmaduro)"}
        if im < IM_OPTIMO_MIN:
            return {"color": "purple", "codigo": "VENCIDA_SOBREMADURO", "label": "🟣 Vencida — seco/sobremaduro"}
        return {"color": "orange", "codigo": "VENCIDA_EN_RANGO", "label": "⚠ Vencida — IM en rango cosecha"}

    # ── Casos próximos
    if dias_a_cos <= ALERTA_DIAS_PROXIMOS:
        if im is None:
            return {"color": "orange", "codigo": "PROXIMA_SIN_DATOS", "label": "🟠 Próxima — sin IM"}
        if im > IM_TEMPRANO:
            return {"color": "red", "codigo": "PROXIMA_INMADURO", "label": "🔴 Próxima — IM alto (inmaduro)"}
        if im < IM_OPTIMO_MIN:
            return {"color": "purple", "codigo": "PROXIMA_SOBREMADURO", "label": "🟣 Próxima — seco/sobremaduro"}
        # Óptimo o Próximo a cosechar
        if im < IM_PROXIMO_MIN:
            return {"color": "green", "codigo": "LISTA_OPTIMO", "label": "🟢 Lista — IM óptimo"}
        return {"color": "green", "codigo": "LISTA_PROXIMO", "label": "🟢 Lista — próximo a cosechar"}

    # ── No próxima: devolver solo clasificación por IM
    return base

def _dias_a_cosecha(fecha_base_str: Optional[str],
                    fechacos_str: Optional[str]) -> Optional[int]:
    """Días entre fecha_base y FECHACOS_PROY (negativo = ya venció)."""
    if not fecha_base_str or not fechacos_str:
        return None
    try:
        d0 = date.fromisoformat(str(fecha_base_str)[:10])
        d1 = date.fromisoformat(str(fechacos_str)[:10])
        return (d1 - d0).days
    except Exception:
        return None


def _tasa_7d_pante(pante1: str) -> Optional[float]:
    """Tasa diaria de IM para un pante individual (últimos 7 días de datos)."""
    sub = PANEL_DIARIO[PANEL_DIARIO["PANTE1"] == pante1]
    if sub.empty:
        return None
    return compute_daily_im_rate(sub, window=7)


def _dias_para_im_objetivo(pante1: str, im_target: float,
                            base_date_str: str, base_age: float) -> Optional[int]:
    """
    Estima cuántos días faltan para que el pante alcance im_target.
    Usa fórmula ganadora si existe, sino PCHIP.
    Retorna None si no cruza o no hay modelo.
    """
    sub = PANEL_DIARIO[PANEL_DIARIO["PANTE1"] == pante1]
    # Requerimiento: para PANTE1 usar polinómica máx grado 3 (sin PCHIP)
    model = fit_model_poly_best(sub, max_deg=3)
    if model is None:
        return None
    xg, yg = model["X_GRID"], model["Y_GRID"]
    edad_obj = invert_for_age(xg, yg, im_target, base_age)
    if edad_obj is None:
        return None
    return int(round(edad_obj - base_age))


def compute_tabla_priorizacion(filters: dict) -> list:
    """
    Genera tabla completa de priorización por pante.
    Columnas:
      PANTE1, FINCA, VARIEDAD, ESTRATO,
      IM_ACTUAL, EDAD_ACTUAL, FECHA_BASE,
      SEMANA_PROY, TERCIO_PROY, FECHACOS_PROY,
      DIAS_A_COSECHA,
      TASA_DIARIA,
      DIAS_A_035, FECHA_EST_035,  # fecha estimada para IM=0.35
      DIAS_A_040, FECHA_EST_040,  # fecha estimada para IM=0.40
      SEMAFORO, PRIORIDAD
    """
    # Aplicar filtros de región/finca/variedad/estrato (sin PANTE1)
    f2 = {k: v for k, v in filters.items() if k != "PANTE1"}
    sample = filter_df(PANEL_DIARIO, f2)

    pantes = [p for p in sample["PANTE1"].unique() if p in _PANTES_ACTIVOS]

    rows = []
    for p in pantes:
        sub = sample[sample["PANTE1"] == p].copy()
        if sub.empty:
            continue

        im_now, fecha_now = compute_im_actual(sub)
        edad_now          = compute_edad_actual(sub)
        tasa              = _tasa_7d_pante(p)

        fecha_base_str = safe_date_str(fecha_now) if fecha_now is not None else None
        bal = get_balance_row(p)
        fechacos = str(bal.get("FECHACOS_PROY","") or "")[:10] or None
        semana   = bal.get("SEMANA_PROY")
        tercio   = bal.get("TERCIO_PROY")
        dias_cos = _dias_a_cosecha(fecha_base_str, fechacos)

        # Metadatos del pante (tomar última fila disponible)
        meta = sub.iloc[-1]
        finca    = str(meta.get("NOMFIN_HIST_MODE","") or "").strip()
        variedad = str(meta.get("VARIE_HIST_MODE","") or "").strip()
        estrato  = str(meta.get("ESTRATO_HIST_MODE","") or "").strip()

        # Días estimados para IM objetivo (0.35 y 0.40)
        def _est(im_tgt):
            if im_now is None or edad_now is None or fecha_base_str is None:
                return None, None
            d = _dias_para_im_objetivo(p, im_tgt, fecha_base_str, edad_now)
            if d is None:
                return None, None
            try:
                fech = (date.fromisoformat(fecha_base_str) +
                        __import__("datetime").timedelta(days=d)).isoformat()
            except Exception:
                fech = None
            return d, fech

        dias_035, fecha_035 = _est(0.35)
        dias_040, fecha_040 = _est(0.40)

        sem = _semaforo(im_now, dias_cos)

        # Prioridad numérica (1=más urgente) basada en semáforo + IM
        prio_map = {
            "VENCIDA_BAJO_IM": 1, "URGENTE": 2, "VENCIDA_OK": 3,
            "ADVERTENCIA": 4, "LISTA": 5, "SOBREMADUREZ": 6,
            "OPTIMO": 7, "ACEPTABLE": 8, "EN_DESARROLLO": 9,
            "TEMPRANO": 10, "SIN_DATOS": 11,
        }
        prioridad = prio_map.get(sem["codigo"], 99)

        rows.append({
            "PANTE1":        p,
            "FINCA":         finca,
            "VARIEDAD":      variedad,
            "ESTRATO":       estrato,
            "IM_ACTUAL":     safe_float(im_now),
            "EDAD_ACTUAL":   safe_float(edad_now),
            "FECHA_BASE":    fecha_base_str,
            "SEMANA_PROY":   semana,
            "TERCIO_PROY":   tercio,
            "FECHACOS_PROY": fechacos,
            "DIAS_A_COSECHA":dias_cos,
            "TASA_DIARIA":   safe_float(tasa),
            "DIAS_A_035":    dias_035,
            "FECHA_EST_035": fecha_035,
            "DIAS_A_040":    dias_040,
            "FECHA_EST_040": fecha_040,
            "SEMAFORO":      sem,
            "PRIORIDAD":     prioridad,
        })

    # Ordenar: primero más urgentes, luego por días a cosecha asc
    rows.sort(key=lambda r: (
        r["PRIORIDAD"],
        r["DIAS_A_COSECHA"] if r["DIAS_A_COSECHA"] is not None else 9999,
        -(r["IM_ACTUAL"] or 0),
    ))
    return rows


def compute_alertas(filters: dict) -> dict:
    """Resume alertas agrupadas por código de semáforo."""
    tabla = compute_tabla_priorizacion(filters)
    grupos = {}
    for r in tabla:
        cod = r["SEMAFORO"]["codigo"]
        if cod not in grupos:
            grupos[cod] = {
                "codigo": cod,
                "label":  r["SEMAFORO"]["label"],
                "color":  r["SEMAFORO"]["color"],
                "pantes": [],
            }
        grupos[cod]["pantes"].append({
            "PANTE1":        r["PANTE1"],
            "FINCA":         r["FINCA"],
            "IM_ACTUAL":     r["IM_ACTUAL"],
            "DIAS_A_COSECHA":r["DIAS_A_COSECHA"],
            "FECHACOS_PROY": r["FECHACOS_PROY"],
            "TASA_DIARIA":   r["TASA_DIARIA"],
        })
    # Orden de presentación
    orden = ["VENCIDA_BAJO_IM","URGENTE","VENCIDA_OK","ADVERTENCIA",
             "LISTA","SOBREMADUREZ","OPTIMO","ACEPTABLE","EN_DESARROLLO","TEMPRANO"]
    alertas_list = [grupos[c] for c in orden if c in grupos]
    # Resumen numérico
    resumen = {
        "total_pantes":    len(tabla),
        "criticos":        sum(1 for r in tabla if r["SEMAFORO"]["color"] in ("red","purple")),
        "advertencia":     sum(1 for r in tabla if r["SEMAFORO"]["color"] == "orange"),
        "listos":          sum(1 for r in tabla if r["SEMAFORO"]["color"] == "green"),
        "en_desarrollo":   sum(1 for r in tabla if r["SEMAFORO"]["color"] in ("yellow","gray")),
    }
    return {"alertas": alertas_list, "resumen": resumen}


def compute_simulador_im(im_target: float, filters: dict) -> list:
    """
    Para todos los pantes del subconjunto filtrado,
    calcula cuándo alcanzarán im_target.
    Devuelve lista ordenada por fecha estimada.
    """
    f2 = {k: v for k, v in filters.items() if k != "PANTE1"}
    sample = filter_df(PANEL_DIARIO, f2)
    pantes = [p for p in sample["PANTE1"].unique() if p in _PANTES_ACTIVOS]

    rows = []
    for p in pantes:
        sub = sample[sample["PANTE1"] == p]
        im_now, fecha_now = compute_im_actual(sub)
        edad_now = compute_edad_actual(sub)
        if im_now is None or edad_now is None or fecha_now is None:
            continue
        fecha_base_str = safe_date_str(fecha_now)
        d = _dias_para_im_objetivo(p, im_target, fecha_base_str, edad_now)
        if d is None:
            continue
        try:
            fecha_est = (date.fromisoformat(fecha_base_str) +
                         __import__("datetime").timedelta(days=d)).isoformat()
        except Exception:
            fecha_est = None
        bal = get_balance_row(p)
        fechacos = str(bal.get("FECHACOS_PROY","") or "")[:10] or None
        meta = sub.iloc[-1]
        rows.append({
            "PANTE1":        p,
            "FINCA":         str(meta.get("NOMFIN_HIST_MODE","") or "").strip(),
            "VARIEDAD":      str(meta.get("VARIE_HIST_MODE","") or "").strip(),
            "IM_ACTUAL":     safe_float(im_now),
            "DIAS_FALTAN":   d,
            "FECHA_ESTIMADA":fecha_est,
            "FECHACOS_PROY": fechacos,
            "DELTA_DIAS":    _dias_a_cosecha(fecha_est, fechacos) if fecha_est and fechacos else None,
            # negativo = llega después de programada → atraso
        })

    rows.sort(key=lambda r: (r["DIAS_FALTAN"] if r["DIAS_FALTAN"] is not None else 9999))
    return rows


# -----------------------------
# FLASK
# -----------------------------
app = Flask(__name__)

def err(msg: str, code: int = 400):
    return jsonify({"error": msg}), code

@app.get("/")
def index():
    return Response(HTML_PAGE, mimetype="text/html")

@app.post("/api/bootstrap")
def api_bootstrap():
    try:
        opts = cascaded_options(PANEL_DIARIO, {})
        opts.update(get_balance_options())
        payload = cached_apply({})
        return jsonify({"options": opts, "payload": payload})
    except Exception as e:
        return err(f"Bootstrap error: {e}", 500)

@app.get("/api/status")
def api_status():
    """Endpoint para verificar estado del sistema y warm up."""
    try:
        status = {
            "warm_up_success": WARM_UP_SUCCESS,
            "panel_diario_loaded": not PANEL_DIARIO.empty,
            "panel_resumen_loaded": not PANEL_RESUMEN.empty,
            "panel_balance_loaded": not PANEL_BALANCE.empty,
            "pantes_activos_count": len(_PANTES_ACTIVOS) if '_PANTES_ACTIVOS' in globals() else 0,
            "panel_diario_rows": len(PANEL_DIARIO),
            "panel_resumen_rows": len(PANEL_RESUMEN),
            "panel_balance_rows": len(PANEL_BALANCE),
            "files_exist": {
                "diario": DEFAULT_DIARIO.exists(),
                "resumen": DEFAULT_RESUMEN.exists(),
                "balance": DEFAULT_BALANCE.exists(),
                "shapefile": SHAPEFILE_PATH.exists()
            }
        }
        return jsonify(status)
    except Exception as e:
        return jsonify({"error": str(e), "warm_up_success": False})

@app.post("/api/options")
def api_options():
    try:
        body = request.get_json(force=True) or {}
        filters = body.get("filters", {}) or {}
        bopts = cascaded_options(PANEL_DIARIO, filters)
        bopts.update(get_balance_options())
        return jsonify({"options": bopts})
    except Exception as e:
        return err(f"Options error: {e}", 500)

@app.post("/api/balance")
def api_balance():
    """Retorna datos del balance de cosecha para un PANTE1 dado."""
    try:
        body = request.get_json(force=True) or {}
        pante1 = str(body.get("pante1","")).strip()
        if not pante1:
            return jsonify({"balance": {}, "has_balance": False})
        bal = get_balance_row(pante1)
        return jsonify({
            "balance": bal,
            "has_balance": bool(bal.get("FECHACOS_PROY")),
            "pante1_key_used": _normalize_pante1_str(pante1),
            "balance_total_pantes": len(_BALANCE_IDX),
        })
    except Exception as e:
        return err(f"Balance error: {e}", 500)

@app.post("/api/apply")
def api_apply():
    try:
        body = request.get_json(force=True) or {}
        filters = body.get("filters", {}) or {}
        # Validación: permitir solo PANTE1 NO COSECHADOS
        psel = str((filters or {}).get("PANTE1","") or "").strip().upper()
        if psel and "_PANTES_NO_COSECHADOS" in globals() and _PANTES_NO_COSECHADOS and (psel not in _PANTES_NO_COSECHADOS):
            return err(f"PANTE1 {psel} aparece como YA COSECHADO (PANTER=1). Solo se permiten NO COSECHADOS (PANTER=0).")
        date_ini = body.get("dateIni", "") or ""
        date_fin = body.get("dateFin", "") or ""
        return jsonify(cached_apply(filters, date_ini, date_fin))
    except Exception as e:
        return err(f"Apply error: {e}", 500)

@app.post("/api/calc_from_im")
def api_calc_from_im():
    try:
        body = request.get_json(force=True) or {}
        model = body.get("model")
        target_im = body.get("target_im")
        base_age = body.get("base_age", 0.0)
        base_date = body.get("base_date")

        if not model:
            return err("No hay modelo. Aplica filtros primero.")
        if target_im is None:
            return err("Falta target_im.")

        xg, yg = model["X_GRID"], model["Y_GRID"]
        current_age = safe_float(base_age)
        edad = invert_for_age(xg, yg, float(target_im), current_age)
        # Si devuelve NaN, tratar como no encontrado
        if edad is not None and (not isinstance(edad, (int, float)) or (isinstance(edad, float) and (edad != edad))):
            edad = None
        if edad is not None and isinstance(edad, (int, float)) and (not math.isfinite(float(edad))):
            edad = None

        if edad is None:
            yg_valid = [v for v in yg if v is not None and isinstance(v, (int, float)) and not (v != v)]
            im_lo = round(min(yg_valid), 4) if yg_valid else "?"
            im_hi = round(max(yg_valid), 4) if yg_valid else "?"
            return jsonify({
                "STATUS": "UNREACHABLE",
                "MESSAGE": (
                    f"No se encontró cruce para IM={float(target_im):.4f}. "
                    f"La curva cubre [{im_lo} – {im_hi}] y no se encontró cruce dentro del rango permitido. "
                    f"El IM objetivo debe estar dentro del rango decreciente proyectable para este pante/filtros."
                ),
                "EDAD_ESTIMADA": None,
                "DIAS_DESDE_BASE": None,
                "FECHA_ESTIMADA": None,
                "IM_EN_EDAD": None,
            })

        if not base_date:
            base_date = date.today().strftime("%Y-%m-%d")

        d0 = datetime.strptime(base_date, "%Y-%m-%d").date()
        delta_days = int(round(float(edad) - float(base_age or 0)))
        est_date = date.fromordinal(d0.toordinal() + delta_days).strftime("%Y-%m-%d")
        im_en_edad = predict_im_at_age(xg, yg, float(edad))

        return jsonify({
            "EDAD_ESTIMADA": round(float(edad), 1),
            "DIAS_DESDE_BASE": delta_days,
            "FECHA_ESTIMADA": est_date,
            "IM_EN_EDAD": safe_float(im_en_edad),
        })
    except Exception as e:
        return err(f"Calc IM error: {e}", 500)

@app.post("/api/calc_from_date")
def api_calc_from_date():
    try:
        body = request.get_json(force=True) or {}
        model = body.get("model")
        target_date = body.get("target_date")
        base_age = body.get("base_age", 0.0)
        base_date = body.get("base_date")

        if not model:
            return err("No hay modelo. Aplica filtros primero.")
        if not target_date:
            return err("Falta target_date.")
        if not base_date:
            base_date = date.today().strftime("%Y-%m-%d")

        d0 = datetime.strptime(base_date, "%Y-%m-%d").date()
        dt = datetime.strptime(target_date, "%Y-%m-%d").date()
        diff = (dt - d0).days
        edad = float(base_age or 0) + diff

        xg, yg = model["X_GRID"], model["Y_GRID"]
        im = predict_im_at_age(xg, yg, edad)

        return jsonify({
            "EDAD_OBJETIVO": round(float(edad), 1),
            "DIAS_DESDE_BASE": diff,
            "IM_ESTIMADO": safe_float(im),
        })
    except Exception as e:
        return err(f"Calc fecha error: {e}", 500)


@app.post("/api/tabla_priorizacion")
def api_tabla_priorizacion():
    """
    Tabla completa de todos los pantes activos con semáforo y prioridad.
    Acepta los mismos filtros que /api/apply (sin PANTE1).
    """
    try:
        body    = request.get_json(force=True) or {}
        filters = body.get("filters", {}) or {}
        tabla   = compute_tabla_priorizacion(filters)
        return jsonify({"tabla": tabla, "total": len(tabla)})
    except Exception as e:
        import traceback; traceback.print_exc()
        return err(f"Tabla priorización error: {e}", 500)


@app.post("/api/alertas")
def api_alertas():
    """Resumen de alertas agrupado por semáforo."""
    try:
        body    = request.get_json(force=True) or {}
        filters = body.get("filters", {}) or {}
        result  = compute_alertas(filters)
        return jsonify(result)
    except Exception as e:
        return err(f"Alertas error: {e}", 500)


@app.post("/api/simulador_im")
def api_simulador_im():
    """
    Para un IM objetivo, calcula cuándo lo alcanza cada pante del filtro.
    Body: { filters: {...}, im_target: 0.35 }
    """
    try:
        body      = request.get_json(force=True) or {}
        filters   = body.get("filters", {}) or {}
        im_target = body.get("im_target")
        if im_target is None:
            return err("Falta im_target")
        rows = compute_simulador_im(float(im_target), filters)
        return jsonify({"rows": rows, "im_target": im_target, "total": len(rows)})
    except Exception as e:
        return err(f"Simulador error: {e}", 500)


@app.post("/api/tiff")
def api_tiff():
    """
    Carga raster TIFF para un PANTE1 específico.
    Body: { pante1: "0001000801" }
    """
    try:
        if not rasterio or not gpd:
            return jsonify({
                "error": "Dependencias GIS no disponibles",
                "detail": "Se requiere rasterio y geopandas. Instalar con: pip install rasterio geopandas"
            }), 400

        body = request.get_json(force=True) or {}
        pante1 = body.get("pante1", "").strip()
        if not pante1:
            return jsonify({"error": "Parámetro 'pante1' requerido"}), 400

        # Verificar shapefile
        if not SHAPEFILE_PATH.exists():
            return jsonify({
                "error": "Shapefile no encontrado",
                "path": str(SHAPEFILE_PATH),
                "detail": "Verificar ruta configurada en SHAPEFILE_PATH"
            }), 404

        # Cargar shapefile
        try:
            gdf = gpd.read_file(SHAPEFILE_PATH)
        except Exception as e:
            return jsonify({
                "error": "Error al leer shapefile",
                "detail": str(e)
            }), 500

        if 'PANTE1' not in gdf.columns:
            available_cols = list(gdf.columns)
            return jsonify({
                "error": "Columna PANTE1 no encontrada en shapefile",
                "available_columns": available_cols,
                "detail": "El shapefile debe tener una columna llamada 'PANTE1'"
            }), 400

        # Filtrar por pante1
        poly = gdf[gdf['PANTE1'] == pante1]
        if poly.empty:
            available_pantes = sorted(gdf['PANTE1'].unique().tolist())[:10]
            return jsonify({
                "error": f"PANTE1 '{pante1}' no encontrado en shapefile",
                "available_sample": available_pantes,
                "total_pantes": len(gdf),
                "detail": "Verificar que el PANTE1 existe en el shapefile"
            }), 404

        # Buscar archivo TIFF
        # Estrategia: buscar en directorio del shapefile
        tiff_dir = SHAPEFILE_PATH.parent
        possible_tiff_names = [
            f"{pante1}.tif",
            f"{pante1}.tiff",
            f"pante_{pante1}.tif",
            "raster.tif",  # TIFF general
            "im_raster.tif"
        ]
        
        tiff_path = None
        for name in possible_tiff_names:
            candidate = tiff_dir / name
            if candidate.exists():
                tiff_path = candidate
                break
        
        if not tiff_path:
            return jsonify({
                "error": "Archivo TIFF no encontrado",
                "searched_names": possible_tiff_names,
                "search_directory": str(tiff_dir),
                "detail": "Colocar archivo TIFF en directorio del shapefile con nombre '{pante1}.tif'"
            }), 404

        # Procesar TIFF con geometría del pante
        try:
            with rasterio.open(tiff_path) as src:
                geom = [shape(poly.geometry.iloc[0].__geo_interface__)]
                out_image, out_transform = mask(src, geom, crop=True)
                out_meta = src.meta.copy()
                out_meta.update({
                    "driver": "GTiff",
                    "height": out_image.shape[1],
                    "width": out_image.shape[2],
                    "transform": out_transform
                })
                
                # Estadísticas del raster recortado
                band_data = out_image[0]  # Primera banda
                valid_pixels = band_data[band_data != src.nodata] if src.nodata is not None else band_data
                
                stats = {
                    "min": float(valid_pixels.min()) if valid_pixels.size > 0 else None,
                    "max": float(valid_pixels.max()) if valid_pixels.size > 0 else None,
                    "mean": float(valid_pixels.mean()) if valid_pixels.size > 0 else None,
                    "pixels": int(valid_pixels.size)
                }

            return jsonify({
                "success": True,
                "pante1": pante1,
                "tiff_file": tiff_path.name,
                "shape": {
                    "bands": out_image.shape[0],
                    "height": out_image.shape[1],
                    "width": out_image.shape[2]
                },
                "statistics": stats,
                "message": f"Raster procesado exitosamente para PANTE1 {pante1}"
            })
            
        except Exception as e:
            return jsonify({
                "error": "Error al procesar TIFF",
                "detail": str(e),
                "tiff_path": str(tiff_path)
            }), 500
            
    except Exception as e:
        import traceback
        return jsonify({
            "error": "Error inesperado en api_tiff",
            "detail": str(e),
            "traceback": traceback.format_exc()
        }), 500



# ---------------------------------------------------------------
# HTML v7 — Dark agroindustrial premium
# ---------------------------------------------------------------
HTML_PAGE = r"""

<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>IM Dashboard v9 — Índice de Maduración</title>
<link rel="preconnect" href="https://fonts.googleapis.com"/>
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin/>
<link href="https://fonts.googleapis.com/css2?family=DM+Mono:ital,wght@0,300;0,400;0,500;1,400&family=Syne:wght@400;600;700;800&display=swap" rel="stylesheet"/>
<script src="https://cdn.plot.ly/plotly-2.27.0.min.js"></script>
<style>
:root{
  --bg:#08090c;
  --s1:#0d1117;
  --s2:#111820;
  --s3:#0a1018;
  --border:rgba(255,255,255,.07);
  --border2:rgba(255,255,255,.13);
  --text:#dce8f5;
  --muted:rgba(220,232,245,.55);
  --muted2:rgba(220,232,245,.35);
  --g1:#00e5b0;
  --g2:#00c4e0;
  --a1:#ffe566;
  --a2:#ff7b54;
  --r:12px;
  --font-head:'Syne',sans-serif;
  --font-mono:'DM Mono',monospace;
}
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%;min-height:100vh}
body{font-family:var(--font-mono);background:var(--bg);color:var(--text);font-size:13px;line-height:1.5;overflow-x:hidden}

/* ── HEADER ── */
.header{
  padding:12px 20px;
  display:flex;align-items:center;justify-content:space-between;
  border-bottom:1px solid var(--border);
  background:rgba(10,14,20,.92);
  backdrop-filter:blur(14px);
  position:sticky;top:0;z-index:200;
  height:56px;
}
.logo{display:flex;align-items:center;gap:10px}
.logo-icon{
  width:32px;height:32px;
  background:linear-gradient(135deg,var(--g1),var(--g2));
  border-radius:8px;display:flex;align-items:center;justify-content:center;
  font-size:16px;box-shadow:0 0 14px rgba(0,229,176,.3);flex-shrink:0;
}
.logo-text{font-family:var(--font-head);font-size:15px;font-weight:700;letter-spacing:.4px}
.logo-sub{font-size:10px;color:var(--muted)}
.status-badge{
  font-size:11px;padding:5px 12px;border-radius:99px;
  border:1px solid var(--border2);background:rgba(0,229,176,.07);
  color:var(--g1);display:flex;align-items:center;gap:6px;
}
.status-dot{width:7px;height:7px;border-radius:50%;background:var(--g1);
  box-shadow:0 0 8px var(--g1);animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.35}}

/* ── 3-COLUMN LAYOUT ── */
/* LEFT: filtros 240px | CENTRE: KPIs + Calculadora ~420px | RIGHT: chart + model */
.layout{
  display:grid;
  grid-template-columns:240px 400px 1fr;
  min-height:calc(100vh - 56px);
  overflow:hidden;
}

/* ── SHARED PANEL STYLE ── */
.panel{
  border-right:1px solid var(--border);
  background:var(--s1);
  overflow-y:auto;
  display:flex;flex-direction:column;
}
.panel:last-child{border-right:none;background:var(--bg)}
.sec{padding:12px 14px;border-bottom:1px solid var(--border)}
.sec:last-child{border-bottom:none}
.sec-title{
  font-family:var(--font-head);font-size:9px;letter-spacing:1.8px;
  text-transform:uppercase;color:var(--muted2);margin-bottom:10px;
}

/* ── FORM FIELDS ── */
.field{margin-bottom:9px}
.field:last-child{margin-bottom:0}
.field label{
  display:block;font-size:9px;letter-spacing:.8px;text-transform:uppercase;
  color:var(--muted);margin-bottom:3px;
}
select,input[type="date"],input[type="number"]{
  width:100%;padding:7px 9px;border-radius:8px;
  border:1px solid var(--border2);background:#131c26;
  color:var(--text);outline:none;
  font-family:var(--font-mono);font-size:11.5px;
  transition:.15s;-webkit-appearance:none;appearance:none;
  background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='11' height='7' viewBox='0 0 11 7'%3E%3Cpath d='M1 1l4.5 4.5L10 1' stroke='%2300e5b0' stroke-width='1.5' fill='none' stroke-linecap='round'/%3E%3C/svg%3E");
  background-repeat:no-repeat;background-position:right 9px center;
  padding-right:28px;cursor:pointer;
}
input[type="date"],input[type="number"]{background-image:none;padding-right:9px;cursor:text}
select:focus,input:focus{border-color:rgba(0,229,176,.5);background:#0f1a24;box-shadow:0 0 0 3px rgba(0,229,176,.07)}
select:hover{border-color:rgba(0,229,176,.28);background:#162030}
select option{background:#131c26;color:#dce8f5;font-family:'DM Mono',monospace}
select option:checked,select option:hover{background:#00e5b0;color:#08090c}
input[type="date"]::-webkit-calendar-picker-indicator{filter:invert(1) brightness(.6) sepia(1) hue-rotate(100deg);cursor:pointer}
.row2{display:grid;grid-template-columns:1fr 1fr;gap:7px}

/* ── BUTTONS ── */
.btn{
  width:100%;padding:8px 10px;border-radius:8px;
  border:1px solid var(--border2);background:rgba(255,255,255,.05);
  color:var(--text);cursor:pointer;font-family:var(--font-mono);
  font-size:11.5px;font-weight:500;transition:.15s;
  display:flex;align-items:center;justify-content:center;gap:5px;
}
.btn:hover{background:rgba(255,255,255,.09);transform:translateY(-1px)}
.btn:active{transform:none}
.btn:disabled{opacity:.3;cursor:not-allowed;transform:none}
.btn-primary{
  background:linear-gradient(135deg,rgba(0,229,176,.18),rgba(0,196,224,.10));
  border-color:rgba(0,229,176,.35);color:var(--g1);
}
.btn-primary:hover{background:linear-gradient(135deg,rgba(0,229,176,.28),rgba(0,196,224,.16))}
.btn-sec{
  background:linear-gradient(135deg,rgba(255,229,102,.12),rgba(255,123,84,.08));
  border-color:rgba(255,229,102,.28);color:var(--a1);
}
.btn-sec:hover{background:linear-gradient(135deg,rgba(255,229,102,.20),rgba(255,123,84,.13))}
.btn-row{display:grid;grid-template-columns:1fr 1fr;gap:7px;margin-top:8px}
.btn-sm{font-size:11px;padding:6px 8px}

/* ── FILTER STATUS BADGE ── */
.model-src-badge{
  display:inline-flex;align-items:center;gap:5px;
  font-size:10px;padding:3px 8px;border-radius:99px;margin-top:8px;
  border:1px solid;
}
.model-src-badge.formula{background:rgba(255,229,102,.08);border-color:rgba(255,229,102,.3);color:var(--a1)}
.model-src-badge.pchip{background:rgba(0,229,176,.06);border-color:rgba(0,229,176,.25);color:var(--g1)}

/* ── KPI MEGA CARDS (centro) ── */
.kpi-mega{
  background:var(--s3);border:1px solid var(--border);
  border-radius:12px;padding:14px 16px;
  display:flex;flex-direction:column;gap:2px;
  position:relative;overflow:hidden;
}
.kpi-mega::before{
  content:"";position:absolute;top:0;left:0;right:0;height:2px;
  background:linear-gradient(90deg,var(--g1),var(--g2));border-radius:99px;
}
.kpi-mega.amber::before{background:linear-gradient(90deg,var(--a1),var(--a2))}
.kpi-mega.blue::before{background:linear-gradient(90deg,var(--g2),#5b9bff)}
.kpi-mega-label{font-size:9px;letter-spacing:1.2px;text-transform:uppercase;color:var(--muted2)}
.kpi-mega-val{
  font-family:var(--font-head);font-size:34px;font-weight:800;line-height:1;
  margin-top:4px;
  background:linear-gradient(90deg,var(--g1),var(--g2));
  -webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;
}
.kpi-mega-val.amber{background:linear-gradient(90deg,var(--a1),var(--a2));-webkit-background-clip:text;background-clip:text}
.kpi-mega-val.blue{background:linear-gradient(90deg,var(--g2),#5b9bff);-webkit-background-clip:text;background-clip:text}
.kpi-mega-sub{font-size:10px;color:var(--muted2);margin-top:2px}

/* KPI grid 2col */
.kpi-grid2{display:grid;grid-template-columns:1fr 1fr;gap:9px}
/* KPI grid 1col */
.kpi-grid1{display:grid;grid-template-columns:1fr;gap:9px}

/* small kpi for row of 3 */
.kpi-sm{
  background:var(--s3);border:1px solid var(--border);
  border-radius:10px;padding:10px 12px;
}
.kpi-sm-label{font-size:9px;letter-spacing:.8px;text-transform:uppercase;color:var(--muted2)}
.kpi-sm-val{
  font-family:var(--font-head);font-size:18px;font-weight:800;margin-top:3px;
  background:linear-gradient(90deg,var(--g1),var(--g2));
  -webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;
}
.kpi-row3{display:grid;grid-template-columns:repeat(3,1fr);gap:9px}

/* ── RESULTADO CALCULADORA ── */
.calc-result-box{
  background:rgba(0,229,176,.05);border:1px solid rgba(0,229,176,.15);
  border-radius:10px;padding:12px 14px;margin-top:10px;
}
.calc-result-box.err{background:rgba(255,80,80,.05);border-color:rgba(255,80,80,.2)}
.calc-result-row{
  display:flex;justify-content:space-between;align-items:baseline;
  border-bottom:1px solid rgba(255,255,255,.05);padding:5px 0;
  font-size:11.5px;
}
.calc-result-row:last-child{border-bottom:none;padding-bottom:0}
.calc-result-label{color:var(--muted)}
.calc-result-val{
  font-family:var(--font-head);font-size:15px;font-weight:700;color:var(--g1);
}
.calc-result-val.amber{color:var(--a1)}
.calc-msg{font-size:12px;color:var(--muted);line-height:1.5}
.calc-msg b{color:var(--g1)}

/* IM range hint */
.im-hint{
  font-size:10px;color:rgba(0,229,176,.55);
  margin-top:3px;display:none;
}

/* ── RIGHT PANEL ── */
.right-panel{
  padding:14px;display:flex;flex-direction:column;gap:12px;overflow-y:auto;
}

/* chart card */
.chart-card{
  background:var(--s1);border:1px solid var(--border);
  border-radius:var(--r);display:flex;flex-direction:column;overflow:hidden;
}
.chart-hd{
  padding:10px 14px;border-bottom:1px solid var(--border);
  display:flex;align-items:center;justify-content:space-between;
  flex-wrap:wrap;gap:6px;
}
.chart-title{font-family:var(--font-head);font-size:13px;font-weight:700}
.tags{display:flex;gap:5px;flex-wrap:wrap}
.tag{
  font-size:10px;padding:3px 8px;border-radius:99px;
  border:1px solid var(--border2);color:var(--muted);
}
.tag.formula{border-color:rgba(255,229,102,.3);color:var(--a1);background:rgba(255,229,102,.06)}
#plot{height:340px}

/* model card */
.model-card{
  background:var(--s1);border:1px solid var(--border);
  border-radius:var(--r);overflow:hidden;
}
.model-hd{
  padding:9px 14px;border-bottom:1px solid var(--border);
  font-family:var(--font-head);font-size:11px;font-weight:700;
  display:flex;align-items:center;gap:7px;color:var(--muted);
  text-transform:uppercase;letter-spacing:.8px;
}
.model-hd::before{
  content:"";width:7px;height:7px;border-radius:50%;
  background:var(--g1);box-shadow:0 0 6px var(--g1);flex-shrink:0;
}

.model-toggle{
  margin-left:auto;
  padding:6px 10px;
  font-size:10px;
  border-radius:999px;
  border:1px solid var(--border);
  background:rgba(255,255,255,.04);
  color:var(--muted);
  cursor:pointer;
}
.model-toggle:hover{background:rgba(255,255,255,.07);color:var(--fg)}
.model-panel.collapsed{display:none}

pre.model-pre{
  padding:10px 14px;
  font-family:var(--font-mono);font-size:10.5px;
  color:var(--muted);line-height:1.55;white-space:pre-wrap;word-break:break-all;
}

/* ── LOADING DOTS ── */
.dots{display:inline-flex;gap:3px;vertical-align:middle}
.dots span{
  width:5px;height:5px;border-radius:50%;background:var(--g1);opacity:.4;
  animation:bd .8s infinite ease-in-out;
}
.dots span:nth-child(2){animation-delay:.12s}
.dots span:nth-child(3){animation-delay:.24s}
@keyframes bd{0%,100%{transform:translateY(0);opacity:.25}50%{transform:translateY(-4px);opacity:1}}

/* ── SCROLLBAR ── */
::-webkit-scrollbar{width:4px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:rgba(255,255,255,.1);border-radius:99px}

/* ── DIVIDER ── */
.divider{height:1px;background:var(--border);margin:4px 0}

/* ── RESPONSIVE ── */
@media(max-width:1100px){
  .layout{grid-template-columns:220px 360px 1fr}
}
@media(max-width:820px){
  .layout{grid-template-columns:1fr}
  .panel{border-right:none;border-bottom:1px solid var(--border)}
}

/* ── MODEL DETAILS PANEL (KaTeX-based) ── */
.model-panel{padding:14px 16px;display:flex;flex-direction:column;gap:14px}
.model-section{display:flex;flex-direction:column;gap:4px}
.model-section-label{
  font-size:9px;letter-spacing:1.4px;text-transform:uppercase;
  color:var(--muted2);border-bottom:1px solid var(--border);
  padding-bottom:4px;margin-bottom:2px;
}
.model-kv{display:flex;justify-content:space-between;align-items:center;
  padding:3px 0;border-bottom:1px solid rgba(255,255,255,.04)}
.model-kv:last-child{border-bottom:none}
.model-kv-key{font-size:10px;color:var(--muted);min-width:80px}
.model-kv-val{font-size:11px;color:var(--text);text-align:right;font-family:var(--font-mono)}
.model-kv-val.green{color:var(--g1)}
.model-kv-val.amber{color:var(--a1)}
/* KaTeX formula container */
.formula-box{
  background:rgba(0,0,0,.25);border:1px solid var(--border);
  border-radius:8px;padding:12px 16px;overflow-x:auto;
  display:flex;align-items:center;justify-content:center;min-height:48px;
}
.katex{font-size:1.05em!important}
.katex-display{margin:0!important}

/* ── DAILY RATE CHIP ── */
.rate-chip{
  display:inline-flex;align-items:center;gap:6px;
  background:rgba(0,229,176,.07);border:1px solid rgba(0,229,176,.2);
  border-radius:8px;padding:7px 11px;margin-top:6px;font-size:11px;
}
.rate-chip.down{background:rgba(255,229,102,.07);border-color:rgba(255,229,102,.25)}
.rate-chip-icon{font-size:14px}
.rate-chip-val{font-family:var(--font-head);font-weight:700;font-size:14px;color:var(--g1)}
.rate-chip-val.down{color:var(--a1)}
.rate-chip-label{color:var(--muted);font-size:10px}

/* ── COMPARISON DELTA in calc results ── */
.delta-row{
  margin-top:8px;border-top:1px solid rgba(255,255,255,.07);
  padding-top:8px;display:flex;flex-direction:column;gap:4px;
}
.delta-item{
  display:flex;justify-content:space-between;align-items:center;
  font-size:11px;padding:2px 0;
}
.delta-label{color:var(--muted)}
.delta-val{font-family:var(--font-head);font-weight:700;font-size:13px}
.delta-val.early{color:#4ecb71}
.delta-val.late{color:#ff7b54}
.delta-val.ontime{color:var(--g1)}
.delta-val.neutral{color:var(--muted)}

/* ═══════ TABS ═══════ */
.tabs-bar{
  display:flex;gap:2px;padding:8px 12px 0;
  border-bottom:1px solid var(--border);background:var(--s1);
  position:sticky;top:56px;z-index:100;
}
.tab-btn{
  font-family:var(--font-head);font-size:10px;letter-spacing:.8px;
  text-transform:uppercase;padding:7px 14px;border-radius:8px 8px 0 0;
  border:1px solid transparent;border-bottom:none;cursor:pointer;
  background:transparent;color:var(--muted);transition:.15s;
  display:flex;align-items:center;gap:5px;
}
.tab-btn:hover{color:var(--text);background:rgba(255,255,255,.04)}
.tab-btn.active{
  background:var(--s2);color:var(--g1);
  border-color:var(--border);border-bottom-color:var(--s2);
}
.tab-panel{display:none}
.tab-panel.active{display:block}

/* ═══════ ALERT CARDS ═══════ */
.alert-group{
  border-radius:10px;overflow:hidden;
  border:1px solid var(--border);margin-bottom:10px;
}
.alert-header{
  display:flex;align-items:center;justify-content:space-between;
  padding:9px 14px;cursor:pointer;user-select:none;
}
.alert-header:hover{filter:brightness(1.1)}
.alert-badge{
  font-size:10px;font-weight:700;padding:2px 8px;
  border-radius:99px;background:rgba(0,0,0,.3);
}
.alert-body{display:none;padding:8px 14px 12px}
.alert-body.open{display:block}
.alert-mini-table{width:100%;border-collapse:collapse;font-size:11px}
.alert-mini-table th{
  text-align:left;font-size:9px;letter-spacing:.9px;text-transform:uppercase;
  color:var(--muted2);padding:4px 6px;border-bottom:1px solid var(--border);
}
.alert-mini-table td{
  padding:5px 6px;border-bottom:1px solid rgba(255,255,255,.04);
  font-family:var(--font-mono);
}
.alert-mini-table tr:last-child td{border-bottom:none}
.alert-mini-table tr:hover td{background:rgba(255,255,255,.03)}

/* semáforo colores */
.sem-red   {color:#ff6b6b}
.sem-orange{color:#ff7b54}
.sem-yellow{color:#ffe566}
.sem-green {color:#4ecb71}
.sem-teal  {color:#00e5b0}
.sem-purple{color:#c084fc}
.sem-gray  {color:rgba(220,232,245,.4)}

/* ═══════ PRIORIDAD TABLE ═══════ */
.prio-toolbar{
  display:flex;align-items:center;gap:8px;
  padding:10px 14px;border-bottom:1px solid var(--border);
  flex-wrap:wrap;
}
.prio-search{
  flex:1;min-width:130px;padding:6px 10px;border-radius:8px;
  border:1px solid var(--border2);background:#131c26;
  color:var(--text);font-family:var(--font-mono);font-size:11px;outline:none;
}
.prio-search:focus{border-color:rgba(0,229,176,.4)}
.prio-filter-chip{
  font-size:10px;padding:4px 10px;border-radius:99px;
  border:1px solid var(--border2);background:rgba(255,255,255,.04);
  color:var(--muted);cursor:pointer;transition:.12s;
}
.prio-filter-chip:hover,.prio-filter-chip.active{
  background:rgba(0,229,176,.12);border-color:rgba(0,229,176,.3);color:var(--g1);
}
.prio-table-wrap{overflow-x:auto;max-height:calc(100vh - 220px)}
.prio-table{width:100%;border-collapse:collapse;font-size:11.5px}
.prio-table th{
  position:sticky;top:0;z-index:10;
  text-align:left;font-size:9px;letter-spacing:.9px;text-transform:uppercase;
  color:var(--muted2);padding:8px 10px;
  background:var(--s1);border-bottom:2px solid var(--border2);
  cursor:pointer;user-select:none;white-space:nowrap;
}
.prio-table th:hover{color:var(--text)}
.prio-table th .sort-arrow{margin-left:3px;opacity:.4;font-size:9px}
.prio-table th.sorted .sort-arrow{opacity:1;color:var(--g1)}
.prio-table td{
  padding:7px 10px;border-bottom:1px solid rgba(255,255,255,.04);
  white-space:nowrap;font-family:var(--font-mono);
}
.prio-table tr:hover td{background:rgba(255,255,255,.025)}
.prio-table tr.row-selected td{background:rgba(0,229,176,.06)}
.prio-num{text-align:right}
.prio-badge{
  display:inline-block;padding:2px 7px;border-radius:99px;
  font-size:10px;font-weight:600;
}
.prio-row-urgent td:first-child{border-left:3px solid #ff6b6b}
.prio-row-warn td:first-child{border-left:3px solid #ff7b54}
.prio-row-ok td:first-child{border-left:3px solid #4ecb71}
.prio-row-dev td:first-child{border-left:3px solid #ffe566}
.prio-row-gray td:first-child{border-left:3px solid rgba(220,232,245,.2)}

/* ═══════ RESUMEN CARDS (alertas) ═══════ */
.resumen-chips{display:flex;gap:8px;flex-wrap:wrap;padding:10px 14px}
.resumen-chip{
  background:rgba(0,0,0,.3);border:1px solid var(--border);
  border-radius:10px;padding:10px 14px;min-width:90px;text-align:center;
}
.resumen-chip-n{
  font-family:var(--font-head);font-weight:800;font-size:24px;line-height:1;
}
.resumen-chip-label{font-size:9px;text-transform:uppercase;letter-spacing:.8px;color:var(--muted2);margin-top:3px}

/* ═══════ SIMULADOR ═══════ */
.sim-control{padding:12px 14px;border-bottom:1px solid var(--border)}
.sim-result-card{
  border:1px solid var(--border);border-radius:10px;
  overflow:hidden;margin:10px 14px;
}
.sim-result-hd{
  padding:8px 12px;font-size:9px;letter-spacing:1px;text-transform:uppercase;
  color:var(--muted2);border-bottom:1px solid var(--border);
  display:flex;align-items:center;justify-content:space-between;
}
.sim-bar-wrap{height:5px;background:rgba(255,255,255,.06);border-radius:99px;overflow:hidden;flex:1;margin-left:8px}
.sim-bar{height:100%;background:linear-gradient(90deg,var(--g1),var(--g2));border-radius:99px;transition:.4s}

/* ═══════ TOOLTIP ═══════ */
.has-tip{position:relative;cursor:help;border-bottom:1px dotted rgba(220,232,245,.3)}
.has-tip:hover::after{
  content:attr(data-tip);position:absolute;bottom:120%;left:0;
  background:#1a2636;border:1px solid var(--border2);color:var(--text);
  padding:5px 9px;border-radius:6px;font-size:10px;line-height:1.4;
  white-space:pre;z-index:999;min-width:180px;pointer-events:none;
  box-shadow:0 4px 16px rgba(0,0,0,.4);
}

/* ═══════ EXPORT BTN ═══════ */
.btn-export{
  font-size:10px;padding:4px 10px;border-radius:6px;
  border:1px solid var(--border2);background:rgba(255,255,255,.04);
  color:var(--muted);cursor:pointer;transition:.12s;
}
.btn-export:hover{background:rgba(0,229,176,.1);color:var(--g1);border-color:rgba(0,229,176,.3)}

</style>
<!-- KaTeX for mathematical formula rendering -->
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/katex.min.css"
  integrity="sha384-n8MVd4RsNIU0tAv4ct0nTaAbDJwPJzDEaqSD1odI+WdtXRGWt2kTvGFasHpSy3SV" crossorigin="anonymous"/>
<script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/katex.min.js"
  integrity="sha384-XjKyOOlGwcjNTAIQHIpgOno0Hl1YQqzUOEleOLALmuqehneUG+vnGctmUb0ZY0l8" crossorigin="anonymous"></script>
</head>
<body>

<!-- HEADER -->
<header class="header">
  <div class="logo">
    <div class="logo-icon">🌾</div>
    <div>
      <div class="logo-text">IM Dashboard v9</div>
      <div class="logo-sub">Índice de Maduración — Proyección por días</div>
    </div>
  </div>
  <div class="status-badge">
    <span class="status-dot"></span>
    <span id="statusTxt">Iniciando…</span>
  </div>
</header>

<div class="layout">

    <!-- ════════════════════════════════════════
       COLUMNA 1 — FILTROS (240px)
  ════════════════════════════════════════ -->
  <aside class="panel">
    <div class="sec">
      <div class="sec-title">Segmentación</div>
      <div class="field">
        <label>COSNXS (IM NAX) 🎯</label>
        <select id="f_cosnxs"></select>
      </div>
      <div class="field">
        <label>Región</label>
        <select id="f_region"></select>
      </div>
      <div class="field">
        <label>Finca</label>
        <select id="f_finca"></select>
      </div>
      <div class="row2">
        <div class="field">
          <label>Variedad</label>
          <select id="f_varie"></select>
        </div>
        <div class="field">
          <label>Estrato</label>
          <select id="f_estrato"></select>
        </div>
      </div>
      <div class="field">
        <label>Pante ✦ activos (no cosechados)</label>
        <select id="f_pante1"></select>
      </div>
      <div class="divider" style="margin:8px 0"></div>
      <div class="sec-title" style="margin-bottom:6px">Logística cosecha</div>
      <div class="row2">
        <div class="field">
          <label>Semana proy.</label>
          <select id="f_semana"></select>
        </div>
        <div class="field">
          <label>Tercio proy.</label>
          <select id="f_tercio"></select>
        </div>
      </div>
      <div class="row2">
        <div class="field">
          <label>Fecha inicio</label>
          <input type="date" id="date_ini"/>
        </div>
        <div class="field">
          <label>Fecha fin</label>
          <input type="date" id="date_fin"/>
        </div>
      </div>
      <div class="btn-row" style="margin-top:10px">
        <button class="btn btn-primary" id="btn_apply">▶ Aplicar</button>
        <button class="btn" id="btn_reset">↺ Reset</button>
      </div>
    </div>

    <!-- MODEL SOURCE INDICATOR -->
    <div class="sec">
      <div class="sec-title">Fuente del modelo</div>
      <div id="model_src_badge" class="model-src-badge pchip">
        <span>◈</span><span id="model_src_txt">PCHIP (sin PANTE1)</span>
      </div>
      <div style="margin-top:8px;font-size:10px;color:var(--muted2);line-height:1.5">
        Selecciona un PANTE1 específico para usar la fórmula ganadora del modelo histórico.
      </div>
    </div>

    <!-- EXPORTAR -->
    <div class="sec">
      <div class="sec-title">Exportar</div>
      <div class="btn-row">
        <button class="btn btn-sm" id="btn_csv">↓ CSV</button>
        <button class="btn btn-sm" id="btn_png">↓ PNG</button>
      </div>
    </div>
  </aside>

  <!-- ════════════════════════════════════════
       COLUMNA 2 — KPIs + CALCULADORA (400px)
  ════════════════════════════════════════ -->
  <section class="panel">

    <!-- KPIs PRINCIPALES -->
    <div class="sec">
      <div class="sec-title">Estado actual del campo</div>

      <!-- IM + EDAD: los dos más importantes, grandes -->
      <div class="kpi-grid2" style="margin-bottom:9px">
        <div class="kpi-mega">
          <div class="kpi-mega-label">IM Actual</div>
          <div class="kpi-mega-val" id="kpi_im">—</div>
          <div class="kpi-mega-sub">Índice de maduración</div>
        </div>
        <div class="kpi-mega amber">
          <div class="kpi-mega-label">Edad actual</div>
          <div class="kpi-mega-val amber" id="kpi_edad">—</div>
          <div class="kpi-mega-sub">días desde siembra</div>
        </div>
      </div>

      <!-- Fecha base -->
      <div class="kpi-mega blue" style="margin-bottom:9px">
        <div class="kpi-mega-label">Fecha base de cálculo</div>
        <div class="kpi-mega-val blue" style="font-size:22px" id="kpi_fecha">—</div>
        <div class="kpi-mega-sub" id="kpi_modelo_activo">Modelo: —</div>
      </div>

      <!-- Fila de 3: N registros, N parcelas, rango edad -->
      <div class="kpi-row3">
        <div class="kpi-sm">
          <div class="kpi-sm-label">Registros</div>
          <div class="kpi-sm-val" id="kpi_n">—</div>
        </div>
        <div class="kpi-sm">
          <div class="kpi-sm-label">Parcelas</div>
          <div class="kpi-sm-val" id="kpi_pantes">—</div>
        </div>
        <div class="kpi-sm">
          <div class="kpi-sm-label">Rango edad</div>
          <div class="kpi-sm-val" style="font-size:13px" id="kpi_rango">—</div>
        </div>
      </div>
    </div>

    <!-- BALANCE COSECHA: solo visible si PANTE1 seleccionado y balance disponible -->
    <div id="kpi_balance_wrap" style="display:none" class="sec">
      <div class="sec-title">📅 Cosecha proyectada — logística</div>
      <div style="background:linear-gradient(135deg,rgba(255,123,84,.10),rgba(255,229,102,.07));
        border:1px solid rgba(255,123,84,.30);border-radius:10px;padding:12px 14px;
        position:relative;overflow:hidden">
        <div style="position:absolute;top:0;left:0;right:0;height:2px;
          background:linear-gradient(90deg,#ff7b54,#ffe566)"></div>
        <div style="font-size:9px;letter-spacing:1px;text-transform:uppercase;
          color:rgba(255,229,102,.55);margin-bottom:3px">Fecha de cosecha programada</div>
        <div id="kpi_fechacos" style="font-family:'Syne',sans-serif;font-size:26px;font-weight:800;
          background:linear-gradient(90deg,#ff7b54,#ffe566);
          -webkit-background-clip:text;-webkit-text-fill-color:transparent;
          background-clip:text">—</div>
        <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-top:10px">
          <div style="background:rgba(0,0,0,.2);border-radius:8px;padding:8px 10px">
            <div style="font-size:9px;text-transform:uppercase;letter-spacing:.7px;
              color:rgba(220,232,245,.35)">Semana</div>
            <div id="kpi_semana" style="font-family:'Syne',sans-serif;font-size:17px;
              font-weight:700;color:#ffe566;margin-top:2px">—</div>
          </div>
          <div style="background:rgba(0,0,0,.2);border-radius:8px;padding:8px 10px">
            <div style="font-size:9px;text-transform:uppercase;letter-spacing:.7px;
              color:rgba(220,232,245,.35)">Tercio</div>
            <div id="kpi_tercio" style="font-family:'Syne',sans-serif;font-size:17px;
              font-weight:700;color:#ff7b54;margin-top:2px">—</div>
          </div>
          <div style="background:rgba(0,0,0,.2);border-radius:8px;padding:8px 10px">
            <div style="font-size:9px;text-transform:uppercase;letter-spacing:.7px;
              color:rgba(220,232,245,.35)">Días restantes</div>
            <div id="kpi_dias_cosecha" style="font-family:'Syne',sans-serif;font-size:17px;
              font-weight:700;color:rgba(220,232,245,.8);margin-top:2px">—</div>
          </div>
        </div>
      </div>
    </div>

    <!-- CALCULADORA: IM → DÍAS -->
    <div class="sec">
      <div class="sec-title">Calcular días para llegar a un IM</div>
      <div class="field">
        <label>IM objetivo</label>
        <input type="number" step="0.001" id="im_target" placeholder="Ej: 0.300"/>
        <div class="im-hint" id="im_range_hint">
          Rango curva activa: <span id="im_range_lo">—</span> – <span id="im_range_hi">—</span>
        </div>
      </div>
      <button class="btn btn-primary" id="btn_calc_im" disabled>▶ Calcular días para ese IM</button>
      <div id="calc_out_im" class="calc-result-box" style="display:none"></div>
    </div>

    <!-- CALCULADORA: FECHA → IM -->
    <div class="sec">
      <div class="sec-title">Calcular IM en una fecha futura</div>
      <div class="field">
        <label>Fecha objetivo</label>
        <input type="date" id="fecha_target"/>
      </div>
      <button class="btn btn-sec" id="btn_calc_fecha" disabled>▶ Calcular IM en esa fecha</button>
      <div id="calc_out_fecha" class="calc-result-box" style="display:none"></div>
    </div>

    <!-- RESULTADO GENERAL / ERRORES -->
    <div class="sec">
      <div id="calc_status" class="calc-msg">
        Vista general cargada. Filtra y presiona <b>Aplicar</b>.
      </div>
    </div>

  </section>

  <!-- ════════════════════════════════════════
       COLUMNA 3 — TABS: Curva / Priorización / Alertas / Simulador
  ════════════════════════════════════════ -->
  <div class="right-panel" style="display:flex;flex-direction:column;overflow:hidden">

    <!-- TAB BAR -->
    <div class="tabs-bar">
      <button class="tab-btn active" data-tab="tab_curva">📈 Curva IM</button>
      <button class="tab-btn" data-tab="tab_prio">📋 Priorización</button>
      <button class="tab-btn" data-tab="tab_alertas">🚦 Alertas</button>
      <button class="tab-btn" data-tab="tab_simulador">🎯 Simulador</button>
      <button class="tab-btn" data-tab="tab_tiff">🗺️ Raster TIFF</button>
    </div>

    <!-- ── TAB 1: CURVA IM ── -->
    <div id="tab_curva" class="tab-panel active" style="flex:1;overflow-y:auto">
      <div class="chart-card">
        <div class="chart-hd">
          <span class="chart-title">Curva IM vs Edad (días)</span>
          <div class="tags">
            <span class="tag" id="tag_model">Modelo: —</span>
            <span class="tag" id="tag_r2">R²: —</span>
            <span class="tag" id="tag_rmse">RMSE: —</span>
          </div>
        </div>
        <div id="plot"></div>
      </div>
      <div class="model-card">
        <div class="model-hd">
          <span>Detalles técnicos del modelo</span>
          <button id="model_details_toggle" class="model-toggle" type="button" aria-expanded="false" onclick="toggleModelDetails()">Mostrar</button>
        </div>
        <div id="model_details_panel" class="model-panel collapsed">
          <div style="color:var(--muted);font-size:11px;padding:8px 0">Aplica filtros para ver el modelo.</div>
        </div>
      </div>
    </div>

    <!-- ── TAB 2: PRIORIZACIÓN ── -->
    <div id="tab_prio" class="tab-panel" style="flex:1;overflow:hidden;display:flex;flex-direction:column">
      <div class="prio-toolbar">
        <input class="prio-search" id="prio_search" placeholder="🔍 Buscar pante, finca…" oninput="filterPrioTable()"/>
        <span class="prio-filter-chip active" data-sem="ALL" onclick="setPrioSemFilter(this)">Todos</span>
        <span class="prio-filter-chip" data-sem="red"    onclick="setPrioSemFilter(this)">🔴 Urgente</span>
        <span class="prio-filter-chip" data-sem="orange" onclick="setPrioSemFilter(this)">🟠 Advertencia</span>
        <span class="prio-filter-chip" data-sem="green"  onclick="setPrioSemFilter(this)">🟢 Lista</span>
        <span class="prio-filter-chip" data-sem="teal"   onclick="setPrioSemFilter(this)">🔵 Aceptable</span>
        <span class="prio-filter-chip" data-sem="yellow" onclick="setPrioSemFilter(this)">🟡 Desarrollo</span>
        <button class="btn-export" onclick="exportPrioCSV()">↓ CSV</button>
      </div>
      <div style="padding:6px 14px;font-size:10px;color:var(--muted2);border-bottom:1px solid var(--border)">
        Mostrando <b id="prio_count">—</b> pantes · Ordenar por columna · Click en fila para ver curva
      </div>
      <div class="prio-table-wrap" id="prio_table_wrap">
        <table class="prio-table" id="prio_table">
          <thead>
            <tr>
              <th onclick="sortPrio('PRIORIDAD')">#<span class="sort-arrow">↕</span></th>
              <th onclick="sortPrio('SEMAFORO')">Estado<span class="sort-arrow">↕</span></th>
              <th onclick="sortPrio('PANTE1')">Pante<span class="sort-arrow">↕</span></th>
              <th onclick="sortPrio('FINCA')">Finca<span class="sort-arrow">↕</span></th>
              <th onclick="sortPrio('IM_ACTUAL')" class="prio-num">IM actual<span class="sort-arrow">↕</span></th>
              <th onclick="sortPrio('TASA_DIARIA')" class="prio-num has-tip" data-tip="IM/día en los últimos 7 días">Tasa/día<span class="sort-arrow">↕</span></th>
              <th onclick="sortPrio('DIAS_A_COSECHA')" class="prio-num">Días→Cos.<span class="sort-arrow">↕</span></th>
              <th onclick="sortPrio('FECHACOS_PROY')">Fecha cos.<span class="sort-arrow">↕</span></th>
              <th onclick="sortPrio('DIAS_A_035')" class="prio-num has-tip" data-tip="Días estimados para llegar a IM=0.35">→IM 0.35<span class="sort-arrow">↕</span></th>
              <th onclick="sortPrio('DIAS_A_040')" class="prio-num has-tip" data-tip="Días estimados para llegar a IM=0.40">→IM 0.40<span class="sort-arrow">↕</span></th>
              <th onclick="sortPrio('VARIEDAD')">Variedad<span class="sort-arrow">↕</span></th>
              <th onclick="sortPrio('ESTRATO')">Estrato<span class="sort-arrow">↕</span></th>
            </tr>
          </thead>
          <tbody id="prio_tbody">
            <tr><td colspan="12" style="text-align:center;color:var(--muted);padding:24px">
              Haz clic en <b>Priorización</b> y luego <b>▶ Aplicar</b> para cargar la tabla.
            </td></tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- ── TAB 3: ALERTAS ── -->
    <div id="tab_alertas" class="tab-panel" style="flex:1;overflow-y:auto;padding:12px 14px">
      <div class="resumen-chips" id="alertas_resumen" style="padding:0 0 12px"></div>
      <div id="alertas_body">
        <div style="color:var(--muted);font-size:11px;padding:8px 0">
          Haz clic en <b>🚦 Alertas</b> para cargar el resumen de urgencias.
        </div>
      </div>
    </div>

    <!-- ── TAB 4: SIMULADOR ── -->
    <div id="tab_simulador" class="tab-panel" style="flex:1;overflow-y:auto">
      <div class="sim-control">
        <div class="sec-title" style="margin-bottom:10px">
          🎯 Simulador de cosecha por IM objetivo
        </div>
        <div style="font-size:11px;color:var(--muted2);line-height:1.6;margin-bottom:12px">
          Define un IM objetivo y el sistema calcula cuándo <b>cada pante del filtro activo</b>
          llegará a ese valor — ordenados de más próximo a más lejano.
          Ideal para planificar semanas de cosecha.
        </div>
        <div style="display:flex;gap:8px;align-items:flex-end;flex-wrap:wrap">
          <div class="field" style="flex:1;min-width:120px;margin:0">
            <label>IM objetivo</label>
            <input type="number" step="0.01" min="0.1" max="0.9" id="sim_im_target"
              placeholder="Ej: 0.35" value="0.35"/>
          </div>
          <button class="btn btn-primary" id="btn_sim" style="width:auto;padding:8px 18px">
            ▶ Simular todos los pantes
          </button>
          <button class="btn-export" onclick="exportSimCSV()" id="btn_sim_export" style="display:none">↓ CSV</button>
        </div>
        <div style="display:flex;gap:6px;margin-top:10px;flex-wrap:wrap">
          <span style="font-size:10px;color:var(--muted2)">Accesos rápidos:</span>
          <button class="prio-filter-chip" onclick="document.getElementById('sim_im_target').value='0.28';runSimulador()">IM 0.28</button>
          <button class="prio-filter-chip" onclick="document.getElementById('sim_im_target').value='0.33';runSimulador()">IM 0.33</button>
          <button class="prio-filter-chip active" onclick="document.getElementById('sim_im_target').value='0.35';runSimulador()">IM 0.35</button>
          <button class="prio-filter-chip" onclick="document.getElementById('sim_im_target').value='0.40';runSimulador()">IM 0.40</button>
          <button class="prio-filter-chip" onclick="document.getElementById('sim_im_target').value='0.45';runSimulador()">IM 0.45</button>
        </div>
      </div>
      <div id="sim_results" style="padding:10px 14px">
        <div style="color:var(--muted);font-size:11px">Define un IM y presiona Simular.</div>
      </div>
    </div>

    <!-- ── TAB 5: RASTER TIFF (v8) ── -->
    <div id="tab_tiff" class="tab-panel" style="flex:1;overflow-y:auto">
      <div class="sim-control">
        <div class="sec-title" style="margin-bottom:10px">
          🗺️ Raster TIFF por PANTE1
        </div>
        <div style="font-size:11px;color:var(--muted2);line-height:1.6;margin-bottom:12px">
          Selecciona un PANTE1 y visualiza su raster TIFF recortado con paleta NAX 9 colores.
        </div>
        <div style="display:flex;gap:8px;align-items:flex-end;flex-wrap:wrap">
          <div class="field" style="flex:1;min-width:200px;margin:0">
            <label>Seleccionar PANTE1</label>
            <select id="tiff_pante1">
              <option value="">— Seleccionar PANTE1 —</option>
            </select>
          </div>
          <button class="btn btn-primary" id="btn_load_tiff" style="width:auto;padding:8px 18px">
            📷 Cargar Raster
          </button>
        </div>
      </div>
      <div id="tiff_results" style="padding:10px 14px">
        <div style="color:var(--muted);font-size:11px">Selecciona un PANTE1 y presiona Cargar Raster.</div>
      </div>
    </div>

  </div>

</div><!-- .layout -->

<script>

// 🔥 PÉGALO AQUÍ — AL INICIO DEL SCRIPT

// =======================================================
// FORMATO GLOBAL DE FECHAS → DD/MM/YYYY
// =======================================================

function formatDateDDMMYYYY(value){
  if(!value) return value;

  const isoMatch = /^\d{4}-\d{2}-\d{2}$/;
  if(typeof value === "string" && isoMatch.test(value)){
    const [y,m,d] = value.split("-");
    return `${d}/${m}/${y}`;
  }

  const isoDateTime = /^\d{4}-\d{2}-\d{2}T/;
  if(typeof value === "string" && isoDateTime.test(value)){
    const datePart = value.split("T")[0];
    const [y,m,d] = datePart.split("-");
    return `${d}/${m}/${y}`;
  }

  return value;
}

const originalTextContent = Object.getOwnPropertyDescriptor(Node.prototype, "textContent");

Object.defineProperty(Node.prototype, "textContent", {
  set: function(value){
    if(typeof value === "string"){
      value = formatDateDDMMYYYY(value);
    }
    originalTextContent.set.call(this, value);
  },
  get: originalTextContent.get
});


// =============================================
// ESTADO GLOBAL
// =============================================
let OPTIONS = {};
let LAST_APPLY = null;
let LAST_MODEL = null;
let optTimer = null;
let optAbort = null;

// =============================================
// STATUS
// =============================================
function setStatus(loading, text){
  const el = document.getElementById("statusTxt");
  if(loading){
    el.innerHTML = text + ' <span class="dots"><span></span><span></span><span></span></span>';
  } else {
    el.textContent = text || "Listo";
  }
}

// =============================================
// HELPERS
// =============================================
function fmt(v, d=3){ return (v==null||isNaN(v)) ? "—" : Number(v).toFixed(d); }

function setSelectOptions(id, vals, placeholder, labels){
  // labels: optional {value: "display label"} map (for PANTE1 enriched dropdown)
  const el = document.getElementById(id);
  const prev = el.value;
  el.innerHTML = `<option value="">${placeholder}</option>`;
  (vals||[]).forEach(v=>{
    const o = document.createElement("option");
    o.value = v;
    o.textContent = (labels && labels[v]) ? labels[v] : v;
    el.appendChild(o);
  });
  if(prev && (vals||[]).includes(prev)) el.value = prev;
  else el.value = "";
}

function getFilters(){
  return {
    COSNXS:            document.getElementById("f_cosnxs").value||"",
    REGION_HIST_MODE:  document.getElementById("f_region").value||"",
    NOMFIN_HIST_MODE:  document.getElementById("f_finca").value||"",
    VARIE_HIST_MODE:   document.getElementById("f_varie").value||"",
    ESTRATO_HIST_MODE: document.getElementById("f_estrato").value||"",
    PANTE1:            document.getElementById("f_pante1").value||"",
    SEMANA_PROY:       document.getElementById("f_semana").value||"",
    TERCIO_PROY:       document.getElementById("f_tercio").value||"",
  };
}

// =============================================
// CALC OUTPUT HELPERS — separate boxes per calc
// =============================================
function showCalcResult(boxId, rows, isErr=false){
  const box = document.getElementById(boxId);
  box.style.display = "block";
  if(isErr){
    box.className = "calc-result-box err";
    box.innerHTML = `<div class="calc-msg" style="color:#ff9090">${rows}</div>`;
    return;
  }
  box.className = "calc-result-box";
  box.innerHTML = rows.map(([label,val,cls])=>
    `<div class="calc-result-row">
       <span class="calc-result-label">${label}</span>
       <span class="calc-result-val ${cls||""}">${val}</span>
     </div>`
  ).join("");
}

function setCalcStatus(html){ document.getElementById("calc_status").innerHTML = html; }

// =============================================
// MODEL SOURCE BADGE
// =============================================
function updateModelSrcBadge(model){
  const badge = document.getElementById("model_src_badge");
  const txt   = document.getElementById("model_src_txt");
  const pante = document.getElementById("f_pante1").value||"";
  if(model && model.SOURCE === "formula_ganadora"){
    badge.className = "model-src-badge formula";
    txt.textContent = "✦ Fórmula ganadora: " + (model.MODEL_TYPE||"").toUpperCase();
  } else {
    badge.className = "model-src-badge pchip";
    txt.textContent = pante ? "◈ PCHIP (fallback)" : "◈ PCHIP (sin PANTE1)";
  }
}

// =============================================
// PLOTLY
// =============================================
const PLY = {
  paper_bgcolor:"rgba(0,0,0,0)",plot_bgcolor:"rgba(0,0,0,0)",
  xaxis:{title:{text:"Edad (días)",font:{color:"rgba(220,232,245,.45)",size:10}},
    gridcolor:"rgba(255,255,255,.05)",color:"rgba(220,232,245,.65)",
    tickfont:{family:"DM Mono,monospace",size:10},zeroline:false},
  yaxis:{title:{text:"IM",font:{color:"rgba(220,232,245,.45)",size:10}},
    gridcolor:"rgba(255,255,255,.05)",color:"rgba(220,232,245,.65)",
    tickfont:{family:"DM Mono,monospace",size:10},zeroline:false},
  margin:{t:10,l:50,r:14,b:46},
  legend:{orientation:"h",y:-0.18,font:{color:"rgba(220,232,245,.6)",size:10,family:"DM Mono,monospace"},bgcolor:"rgba(0,0,0,0)"},
  hovermode:"x unified",
  hoverlabel:{bgcolor:"#111820",font:{family:"DM Mono,monospace",size:10}},
};

function plotPayload(payload){
  const x=payload.X_DATA||[], yobs=payload.Y_OBS||[], yint=payload.Y_INTERP||[];
  const m=payload.MODEL, mp=payload.MODEL_PCHIP;
  const traces=[];

  if(x.length){
    traces.push({x,y:yobs,mode:"markers",name:"IM observado",
      marker:{size:4,color:"rgba(0,229,176,.5)"},
      hovertemplate:"Obs: <b>%{y:.4f}</b><extra></extra>"});
    traces.push({x,y:yint,mode:"lines",name:"IM interpolado",
      line:{color:"rgba(0,196,224,.5)",width:1.5,dash:"dot"},
      hovertemplate:"Interp: <b>%{y:.4f}</b><extra></extra>"});
  }
  if(m){
    const isFormula = m.SOURCE==="formula_ganadora";
    traces.push({
      x:m.X_GRID,y:m.Y_GRID,mode:"lines",
      name: isFormula ? `Fórmula ganadora (${(m.MODEL_TYPE||"").toUpperCase()})` : "Curva PCHIP",
      line:{color: isFormula?"#ffe566":"#00e5b0",width:3},
      hovertemplate:`<b>${isFormula?"Fórmula":"PCHIP"}:</b> %{y:.4f} @ día %{x:.0f}<extra></extra>`
    });
  }
  if(mp && m && m.SOURCE==="formula_ganadora"){
    traces.push({
      x:mp.X_GRID,y:mp.Y_GRID,mode:"lines",name:"PCHIP (referencia)",
      line:{color:"rgba(0,229,176,.35)",width:1.5,dash:"dot"},
      hovertemplate:"PCHIP ref: <b>%{y:.4f}</b><extra></extra>"
    });
  }
  if(!traces.length){Plotly.newPlot("plot",[],PLY,{displayModeBar:false,responsive:true});return;}

  // Añadir línea vertical de fecha cosecha proyectada si aplica
  const layout = {...PLY};
  const balData = (typeof LAST_APPLY !== "undefined" && LAST_APPLY) ? (LAST_APPLY.BALANCE||{}) : {};
  const pante1A = (typeof LAST_APPLY !== "undefined" && LAST_APPLY)
    ? ((LAST_APPLY.filters_applied||{}).PANTE1||"") : "";
  if(pante1A && balData.FECHACOS_PROY && payload.FECHA_BASE){
    const d1 = new Date(payload.FECHA_BASE), d2 = new Date(balData.FECHACOS_PROY);
    const diffDays = Math.round((d2-d1)/(1000*60*60*24));
    const edadCos = (payload.EDAD_ACTUAL||0) + diffDays;
    layout.shapes = [{
      type:"line", xref:"x", yref:"paper",
      x0:edadCos, x1:edadCos, y0:0, y1:1,
      line:{color:"rgba(255,123,84,.7)",width:2,dash:"dashdot"}
    }];
    layout.annotations = [{
      x:edadCos, yref:"paper", y:1.0, xanchor:"left",
      text:`🗓 Cos. ${balData.FECHACOS_PROY}`,
      font:{color:"#ff7b54",size:10,family:"DM Mono,monospace"},
      showarrow:false, bgcolor:"rgba(255,123,84,.12)",
      bordercolor:"rgba(255,123,84,.3)", borderpad:3
    }];
  }
  Plotly.newPlot("plot",traces,layout,{displayModeBar:true,responsive:true,
    modeBarButtons:[["zoom2d","zoomIn2d","zoomOut2d","resetScale2d","toImage"]]});
}

// =============================================
// UPDATE UI
// =============================================
function updateUI(payload){
  LAST_APPLY  = payload;
  LAST_MODEL  = payload.MODEL||null;
  const m     = LAST_MODEL;
  const im    = payload.IM_ACTUAL;
  const edad  = payload.EDAD_ACTUAL;
  const n     = payload.N??0;
  const st    = payload.STATS||{};
  const fd    = payload.FECHA_BASE||"—";

  // KPI mega
  document.getElementById("kpi_im").textContent    = im!=null ? fmt(im) : "—";
  document.getElementById("kpi_edad").textContent  = edad!=null ? Math.round(edad) : "—";
  document.getElementById("kpi_fecha").textContent = fd;
  document.getElementById("kpi_n").textContent     = n.toLocaleString();
  document.getElementById("kpi_pantes").textContent= st.N_PANTES??m??"—";
  document.getElementById("kpi_rango").textContent =
    (st.EDAD_MIN!=null&&st.EDAD_MAX!=null)
      ? `${Math.round(st.EDAD_MIN)}–${Math.round(st.EDAD_MAX)}d` : "—";

  const modelLabel = m ? (m.SOURCE==="formula_ganadora"
    ? `✦ ${(m.MODEL_TYPE||"").toUpperCase()} — fórmula histórica`
    : "◈ PCHIP — tiempo real") : "Sin modelo";
  document.getElementById("kpi_modelo_activo").textContent = modelLabel;

  // Chart tags
  document.getElementById("tag_model").textContent =
    "Modelo: " + (m ? (m.SOURCE==="formula_ganadora"
      ? "✦ "+(m.MODEL_TYPE||"").toUpperCase() : "◈ PCHIP") : "—");
  document.getElementById("tag_model").className =
    "tag" + (m&&m.SOURCE==="formula_ganadora" ? " formula" : "");
  document.getElementById("tag_r2").textContent =
    "R²: " + (m&&m.R2!=null ? fmt(m.R2,4) : "—");
  document.getElementById("tag_rmse").textContent =
    "RMSE: " + (m&&m.RMSE!=null ? fmt(m.RMSE,4) : "—");

  // Botones
  document.getElementById("btn_calc_im").disabled    = !m;
  document.getElementById("btn_calc_fecha").disabled  = !m;

  // IM range hint
  const hint = document.getElementById("im_range_hint");
  if(m && m.IM_MIN!=null && m.IM_MAX!=null){
    document.getElementById("im_range_lo").textContent = m.IM_MIN.toFixed(4);
    document.getElementById("im_range_hi").textContent = m.IM_MAX.toFixed(4);
    hint.style.display = "block";
  } else {
    hint.style.display = "none";
  }

  // Model source badge
  updateModelSrcBadge(m);

  // Model details
  const md=[];
  if(m){
    md.push(m.SOURCE==="formula_ganadora"
      ? "✦ FÓRMULA GANADORA (panel_resumen) — usada para calcular"
      : "◈ PCHIP tiempo real — usado para calcular");
    md.push(`MODELO    : ${m.MODEL_NAME}`);
    md.push(`FÓRMULA   : ${m.MODEL_FORMULA}`);
    md.push(`R²        : ${m.R2!=null?fmt(m.R2,4):"NA"}   RMSE: ${m.RMSE!=null?fmt(m.RMSE,4):"NA"}`);
    md.push(`EDAD      : [${m.EDAD_MIN.toFixed(1)} – ${m.EDAD_MAX.toFixed(1)} días]`);
    md.push(`IM rango  : [${m.IM_MIN.toFixed(4)} – ${m.IM_MAX.toFixed(4)}]`);
    const mp=payload.MODEL_PCHIP;
    if(mp&&m.SOURCE==="formula_ganadora"){
      md.push("");
      md.push(`◈ PCHIP (referencia visual): R²=${mp.R2!=null?fmt(mp.R2,4):"NA"}  RMSE=${mp.RMSE!=null?fmt(mp.RMSE,4):"NA"}`);
    }
  } else {
    md.push("Sin modelo (datos insuficientes o subset vacío).");
  }
  const rs=payload.RESUMEN;
  if(rs&&Object.keys(rs).length){
    md.push("");md.push("── RESUMEN PANEL ──────────────────────────────────");
    ["MODEL_USED","MODEL_TYPE","R2","RMSE","MODEL_FORMULA","MODEL_PARAMS",
     "IM_NOW","EDAD_DIAS_FIN","FECHA_PANEL_FIN","N"].forEach(k=>{
      if(rs[k]!=null) md.push(`${k.padEnd(16)}: ${rs[k]}`);
    });
  }
  renderModelDetails(payload);
  plotPayload(payload);

  // ── BALANCE DE COSECHA ── (null-safe)
  const bal = payload.BALANCE||{};
  const pante1Active = (payload.filters_applied||{}).PANTE1||"";
  const balWrap   = document.getElementById("kpi_balance_wrap");
  const elFechacos = document.getElementById("kpi_fechacos");
  const elSemana   = document.getElementById("kpi_semana");
  const elTercio   = document.getElementById("kpi_tercio");
  const elDiasCos  = document.getElementById("kpi_dias_cosecha");

  if(balWrap && pante1Active && bal.FECHACOS_PROY){
    balWrap.style.display = "block";
    if(elFechacos) elFechacos.textContent = bal.FECHACOS_PROY||"—";
    if(elSemana)   elSemana.textContent   = bal.SEMANA_PROY||"—";
    if(elTercio)   elTercio.textContent   = bal.TERCIO_PROY||"—";
    // Calcular días hasta cosecha
    if(elDiasCos){
      const fechaBase = payload.FECHA_BASE;
      const fechaCos  = bal.FECHACOS_PROY;
      if(fechaBase && fechaCos){
        const d1 = new Date(fechaBase), d2 = new Date(fechaCos);
        const diff = Math.round((d2 - d1) / (1000*60*60*24));
        if(isFinite(diff)){
          elDiasCos.textContent = diff >= 0 ? `${diff} días` : `hace ${Math.abs(diff)} días`;
          elDiasCos.style.color = diff < 0 ? "#ff9090" : diff < 14 ? "#ff7b54" : "rgba(220,232,245,.8)";
        } else {
          elDiasCos.textContent = "—";
        }
      }
    }
  } else if(balWrap) {
    balWrap.style.display = "none";
  }
}

// =============================================
// CASCADE OPTIONS (debounce + AbortController)
// =============================================
async function refreshOptionsCascade(){
  if(optTimer) clearTimeout(optTimer);
  optTimer = setTimeout(async()=>{
    if(optAbort) optAbort.abort();
    optAbort = new AbortController();
    try{
      setStatus(true,"Actualizando filtros");
      const resp = await fetch("/api/options",{
        method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({filters:getFilters()}),signal:optAbort.signal
      });
      if(!resp.ok) return;
      const data = await resp.json();
      OPTIONS = data.options||{};
      applySelectMaps();
      setStatus(false,"Listo");
    }catch(e){if(e.name!=="AbortError") setStatus(false,"Listo");}
  },280);
}

function applySelectMaps(){
  [
    ["f_cosnxs",  "COSNXS",            "— Todos —",                null],
    ["f_region",  "REGION_HIST_MODE",  "— Todas las regiones —",   null],
    ["f_finca",   "NOMFIN_HIST_MODE",  "— Todas las fincas —",     null],
    ["f_varie",   "VARIE_HIST_MODE",   "— Todas las variedades —", null],
    ["f_estrato", "ESTRATO_HIST_MODE", "— Todos los estratos —",   null],
    ["f_pante1",  "PANTE1",            "— Todos los pantes (activos) —", OPTIONS["PANTE1_LABELS"]||null],
    ["tiff_pante1","PANTE1",           "— Seleccionar PANTE1 —",       OPTIONS["PANTE1_LABELS"]||null],
    ["f_semana",  "SEMANA_PROY",       "— Todas las semanas —",    null],
    ["f_tercio",  "TERCIO_PROY",       "— Todos los tercios —",    null],
  ].forEach(([id,col,ph,labels])=>setSelectOptions(id, OPTIONS[col]||[], ph, labels));
}

// =============================================
// APPLY FILTERS
// =============================================
async function applyFilters(){
  setStatus(true,"Calculando…");
  document.getElementById("btn_apply").disabled = true;
  // Limpiar resultados anteriores
  ["calc_out_im","calc_out_fecha"].forEach(id=>{
    const el=document.getElementById(id); el.style.display="none"; el.innerHTML="";
  });
  try{
    const resp = await fetch("/api/apply",{
      method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({
        filters:getFilters(),
        dateIni:document.getElementById("date_ini").value||"",
        dateFin:document.getElementById("date_fin").value||""
      })
    });
    const data = await resp.json();
    if(data.error){
      setCalcStatus(`<span style="color:#ff9090">Error: ${data.error}</span>`);
    } else {
      updateUI(data);
      setCalcStatus("Filtros aplicados. Ingresa un IM o fecha para calcular.");
    }
  }catch(e){
    setCalcStatus(`<span style="color:#ff9090">Error de conexión: ${e.message}</span>`);
  }finally{
    setStatus(false,"Listo");
    document.getElementById("btn_apply").disabled = false;
  }
}

// =============================================
// RESET
// =============================================
function resetAll(){
  ["f_cosnxs","f_region","f_finca","f_varie","f_estrato","f_pante1","f_semana","f_tercio"].forEach(id=>{
    document.getElementById(id).value="";
  });
  ["date_ini","date_fin","im_target","fecha_target"].forEach(id=>{
    document.getElementById(id).value="";
  });
  ["calc_out_im","calc_out_fecha"].forEach(id=>{
    const el=document.getElementById(id); el.style.display="none";
  });
  refreshOptionsCascade();
  applyFilters();
}

// =============================================
// HELPERS: date difference and delta display
// =============================================
function dateDiffDays(a, b){
  // Returns (new Date(b)) - (new Date(a)) in integer days
  return Math.round((new Date(b) - new Date(a)) / 86400000);
}

function fmtDate(s){
  // "2026-03-15" → "15/03/2026"
  if(!s) return "—";
  try{ const p=s.split("-"); return `${p[2]}/${p[1]}/${p[0]}`; }
  catch(e){ return s; }
}

function buildDeltaBlock(fechaCos, fechaCalc, labelCalc, imAtCos){
  // fechaCos: FECHACOS_PROY string, fechaCalc: calculated date string
  if(!fechaCos) return "";
  const diff = dateDiffDays(fechaCos, fechaCalc);
  const abs  = Math.abs(diff);
  let cls, arrow, verb;
  if(diff < -1){
    cls="early"; arrow="⬆"; verb=`${abs} días <b>antes</b> de lo programado`;
  } else if(diff > 1){
    cls="late";  arrow="⬇"; verb=`${abs} días <b>después</b> de lo programado`;
  } else {
    cls="ontime"; arrow="✓"; verb="<b>Coincide</b> con la programación (±1 día)";
  }
  const imRef = (imAtCos!=null && isFinite(imAtCos))
    ? `<div class="delta-item" style="margin-top:4px">
        <span class="delta-label">IM en fecha programada</span>
        <span class="delta-val" style="color:rgba(220,232,245,.7)">${fmt(imAtCos,4)}</span>
       </div>` : "";
  return `
    <div class="delta-row">
      <div style="font-size:9px;letter-spacing:1.2px;text-transform:uppercase;
        color:rgba(255,229,102,.6);margin-bottom:6px">
        📅 Comparación vs programación logística
      </div>
      <div class="delta-item">
        <span class="delta-label">Cosecha programada</span>
        <span class="delta-val neutral">${fmtDate(fechaCos)}
          <span style="font-size:10px;font-weight:400;color:var(--muted)"> (semana ${LAST_APPLY.BALANCE?.SEMANA_PROY||"—"})</span>
        </span>
      </div>
      <div class="delta-item">
        <span class="delta-label">${labelCalc}</span>
        <span class="delta-val neutral">${fmtDate(fechaCalc)}</span>
      </div>
      <div class="delta-item" style="padding-top:6px;border-top:1px solid rgba(255,255,255,.08);margin-top:4px">
        <span class="delta-label">Diferencia</span>
        <span class="delta-val ${cls}">${arrow} ${verb}</span>
      </div>
      ${imRef}
    </div>`;
}

function buildRateChip(dr, nDays){
  if(dr==null) return "";
  const isDown = dr<0;
  const sign   = dr>=0?"+":"−";
  const change = (Math.abs(dr)*Math.abs(nDays)).toFixed(4);
  return `<div style="display:flex;align-items:center;gap:8px;margin-top:8px;flex-wrap:wrap">
    <div class="rate-chip ${isDown?"down":""}">
      <span class="rate-chip-icon">${isDown?"📉":"📈"}</span>
      <span class="rate-chip-val ${isDown?"down":""}">${sign}${Math.abs(dr).toFixed(5)}</span>
      <span class="rate-chip-label">IM / día</span>
    </div>
    <div style="font-size:10px;color:var(--muted2);line-height:1.5">
      En <b style="color:var(--text)">${Math.abs(nDays)} días</b>:<br>
      cambio estimado <b style="color:${isDown?"var(--a1)":"var(--g1)"}">${sign}${change} IM</b>
    </div>
  </div>`;
}

// =============================================
// CALC: IM → DÍAS
// =============================================
async function calcDaysFromIM(){
  if(!LAST_MODEL){setCalcStatus("Aplica filtros para obtener un modelo.",false);return;}
  const target = parseFloat(document.getElementById("im_target").value);
  if(!isFinite(target)){
    showCalcResult("calc_out_im","Ingresa un valor de IM válido (ej: 0.350).",true); return;
  }
  const baseAge  = LAST_APPLY.EDAD_ACTUAL??0.0;
  const baseDate = LAST_APPLY.FECHA_BASE||new Date().toISOString().slice(0,10);
  const dr       = LAST_APPLY.DAILY_RATE;
  setStatus(true,"Calculando…");
  try{
    const resp = await fetch("/api/calc_from_im",{
      method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({model:LAST_MODEL,target_im:target,base_age:baseAge,base_date:baseDate})
    });
    const data = await resp.json();
    if(data.error){
      showCalcResult("calc_out_im", `⚠ ${data.error}`, true);
    } else if(data.STATUS==="UNREACHABLE"){
      showCalcResult("calc_out_im", `⚠ ${data.MESSAGE||"No se encontró cruce para ese IM."}`, true);
    } else {
      const box = document.getElementById("calc_out_im");
      box.style.display = "block";
      box.className = "calc-result-box";

      const bal = LAST_APPLY.BALANCE||{};
      // IM que tendría el cultivo EN la fecha programada (como referencia)
      let imAtCos = null;
      if(bal.FECHACOS_PROY && LAST_MODEL){
        const cosAge = baseAge + dateDiffDays(baseDate, bal.FECHACOS_PROY);
        const xg=LAST_MODEL.X_GRID, yg=LAST_MODEL.Y_GRID;
        // simple linear interp for reference
        for(let i=0;i<xg.length-1;i++){
          if(cosAge>=xg[i]&&cosAge<=xg[i+1]){
            const t=(cosAge-xg[i])/(xg[i+1]-xg[i]);
            imAtCos=yg[i]*(1-t)+yg[i+1]*t; break;
          }
        }
      }

      box.innerHTML = `
        <div style="font-size:9px;letter-spacing:1.3px;text-transform:uppercase;
          color:var(--g1);margin-bottom:8px;padding-bottom:6px;
          border-bottom:1px solid rgba(0,229,176,.15)">
          Resultado del cálculo — IM → Fecha
        </div>
        <div class="calc-result-row">
          <span class="calc-result-label">IM objetivo ingresado</span>
          <span class="calc-result-val">${fmt(target,4)}</span>
        </div>
        <div class="calc-result-row">
          <span class="calc-result-label">Edad estimada al llegar</span>
          <span class="calc-result-val">${data.EDAD_ESTIMADA} días</span>
        </div>
        <div class="calc-result-row">
          <span class="calc-result-label">Días desde hoy hasta llegar</span>
          <span class="calc-result-val amber">${data.DIAS_DESDE_BASE >= 0
            ? data.DIAS_DESDE_BASE + " días"
            : "ya alcanzado hace " + Math.abs(data.DIAS_DESDE_BASE) + " días"}</span>
        </div>
        <div class="calc-result-row" style="border-bottom:none">
          <span class="calc-result-label">📅 Fecha estimada de llegada</span>
          <span class="calc-result-val" style="font-size:17px">${fmtDate(data.FECHA_ESTIMADA)}</span>
        </div>
        ${buildRateChip(dr, data.DIAS_DESDE_BASE)}
        ${buildDeltaBlock(bal.FECHACOS_PROY, data.FECHA_ESTIMADA, "Fecha calculada (modelo)", imAtCos)}
      `;
    }
  }catch(e){showCalcResult("calc_out_im",`Error: ${e.message}`,true);}
  finally{setStatus(false,"Listo");}
}

// =============================================
// CALC: FECHA → IM
// =============================================
async function calcIMFromDate(){
  if(!LAST_MODEL){setCalcStatus("Aplica filtros para obtener un modelo.");return;}
  const d = document.getElementById("fecha_target").value;
  if(!d){showCalcResult("calc_out_fecha","Selecciona una fecha objetivo.",true);return;}
  const baseAge  = LAST_APPLY.EDAD_ACTUAL??0.0;
  const baseDate = LAST_APPLY.FECHA_BASE||new Date().toISOString().slice(0,10);
  const dr       = LAST_APPLY.DAILY_RATE;
  setStatus(true,"Calculando…");
  try{
    const resp = await fetch("/api/calc_from_date",{
      method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({model:LAST_MODEL,target_date:d,base_age:baseAge,base_date:baseDate})
    });
    const data = await resp.json();
    if(data.error){
      showCalcResult("calc_out_fecha",`⚠ ${data.error}`,true);
    } else {
      const box = document.getElementById("calc_out_fecha");
      box.style.display = "block";
      box.className = "calc-result-box";

      const bal = LAST_APPLY.BALANCE||{};
      const nDays = data.DIAS_DESDE_BASE;

      // IM at FECHACOS_PROY for reference
      let imAtCos = null;
      if(bal.FECHACOS_PROY && LAST_MODEL){
        const cosAge = baseAge + dateDiffDays(baseDate, bal.FECHACOS_PROY);
        const xg=LAST_MODEL.X_GRID, yg=LAST_MODEL.Y_GRID;
        for(let i=0;i<xg.length-1;i++){
          if(cosAge>=xg[i]&&cosAge<=xg[i+1]){
            const t=(cosAge-xg[i])/(xg[i+1]-xg[i]);
            imAtCos=yg[i]*(1-t)+yg[i+1]*t; break;
          }
        }
      }

      // IM interpretation
      const imVal = data.IM_ESTIMADO;
      let imInterp = "";
      if(imVal!=null){
        if(imVal < 0.28)      imInterp = "⚠ Muy bajo — crecimiento temprano";
        else if(imVal < 0.35) imInterp = "🟡 Bajo — seguir monitoreando";
        else if(imVal < 0.42) imInterp = "🟢 Aceptable para cosecha";
        else if(imVal < 0.55) imInterp = "✅ Óptimo para cosecha";
        else                   imInterp = "⚠ Alto — posible sobre-maduración";
      }

      box.innerHTML = `
        <div style="font-size:9px;letter-spacing:1.3px;text-transform:uppercase;
          color:var(--a1);margin-bottom:8px;padding-bottom:6px;
          border-bottom:1px solid rgba(255,229,102,.15)">
          Resultado del cálculo — Fecha → IM
        </div>
        <div class="calc-result-row">
          <span class="calc-result-label">📅 Fecha objetivo ingresada</span>
          <span class="calc-result-val">${fmtDate(d)}</span>
        </div>
        <div class="calc-result-row">
          <span class="calc-result-label">Días desde hoy hasta esa fecha</span>
          <span class="calc-result-val">${nDays >= 0
            ? nDays + " días"
            : "hace " + Math.abs(nDays) + " días (pasada)"}</span>
        </div>
        <div class="calc-result-row">
          <span class="calc-result-label">Edad en esa fecha</span>
          <span class="calc-result-val">${data.EDAD_OBJETIVO} días</span>
        </div>
        <div class="calc-result-row" style="border-bottom:none">
          <span class="calc-result-label">IM estimado en esa fecha</span>
          <span class="calc-result-val amber" style="font-size:20px">${fmt(imVal,4)}</span>
        </div>
        ${imInterp ? `<div style="font-size:11px;color:var(--muted);margin-top:4px;
          padding:5px 8px;background:rgba(255,255,255,.04);border-radius:6px">
          ${imInterp}</div>` : ""}
        ${buildRateChip(dr, nDays)}
        ${buildDeltaBlock(bal.FECHACOS_PROY, d, "Fecha objetivo ingresada",
          bal.FECHACOS_PROY ? imAtCos : null)}
      `;
    }
  }catch(e){showCalcResult("calc_out_fecha",`Error: ${e.message}`,true);}
  finally{setStatus(false,"Listo");}
}

// =============================================
// EXPORT
// =============================================
function exportCSV(){
  if(!LAST_APPLY){return;}
  const x=LAST_APPLY.X_DATA||[],yobs=LAST_APPLY.Y_OBS||[],yint=LAST_APPLY.Y_INTERP||[];
  const rows=["EDAD_DIAS,IM_OBS,IM_INTERP"];
  for(let i=0;i<x.length;i++) rows.push([x[i]??"",yobs[i]??"",yint[i]??""].join(","));
  const a=document.createElement("a");
  a.href=URL.createObjectURL(new Blob([rows.join("\n")],{type:"text/csv"}));
  a.download=`im_subset_${new Date().toISOString().slice(0,10)}.csv`;a.click();
}
function exportPNG(){
  Plotly.toImage(document.getElementById("plot"),{format:"png",height:600,width:1200})
    .then(url=>{const a=document.createElement("a");a.href=url;a.download="grafica_im.png";a.click();});
}

// =============================================
// RENDER MODEL DETAILS (KaTeX)
// =============================================
function modelFormToKatex(formula, mtype){
  // Convert MODEL_FORMULA string to KaTeX-renderable LaTeX
  if(!formula || formula.startsWith("IM = PchipInterpolator")) {
    return null; // PCHIP has no algebraic formula
  }
  try{
    // Expected format: "IM(t) = a + b*t + c*t^2 + d*t^3 ..."
    // or backend MODEL_FORMULA raw string
    let s = formula
      .replace(/IM\(t\)\s*=/,"")
      .replace(/IM\s*=/,"")
      .trim();

    // Replace scientific notation: 1.23e-05 → 1.23 	imes 10^{-5}
    s = s.replace(/([-+]?\d*\.?\d+)[eE]([-+]?\d+)/g,(_,m,e)=>{
      const ei=parseInt(e);
      if(ei===0) return m;
      return `${m} \times 10^{${ei}}`;
    });

    // t^2 → t^{2}, t^3 → t^{3}
    s = s.replace(/\*?t\^(\d+)/g, (_,n)=>`t^{${n}}`);

    // Polynomial: just prettify
    s = s.replace(/\*/g,"\cdot ");
    s = s.replace(/\+\s*-/g, "- ");

    // Special: exp_decay:  a * exp(b*t) + c
    s = s.replace(/exp\(([^)]+)\)/g, (_,inner)=>{
      const ii = inner.replace(/\*/g,"\cdot ").replace(/\+\s*-/g,"- ");
      return `e^{${ii}}`;
    });

    // log:  a + b * log(t)
    s = s.replace(/log\(([^)]+)\)/g, (_,inner)=>`\ln(${inner})`);

    // gompertz:  a * exp(-b * exp(-c*t))
    // already handled by exp substitution above if nested — ok

    // logistic: L / (1 + exp(-k*(t-t0)))
    s = s.replace(/\s*\/\s*\(\s*1\s*\+\s*(e\^{[^}]+})\s*\)/g,
      (_,exp)=>`\,/\,(1 + ${exp})`);

    // clean up spaces
    s = s.replace(/\s{2,}/g," ").trim();

    return `\mathrm{IM}(t) = ${s}`;
  }catch(e){ return null; }
}

function renderKatex(container, latex){
  try{
    if(typeof katex !== "undefined"){
      katex.render(latex, container, {
        displayMode: true, throwOnError: false,
        output: "html",
        trust: false,
      });
    } else {
      container.textContent = latex;
    }
  }catch(e){ container.textContent = latex; }
}


function toggleModelDetails(force){
  const panel = document.getElementById("model_details_panel");
  const btn   = document.getElementById("model_details_toggle");
  if(!panel || !btn) return;

  let collapsed;
  if(typeof force === "boolean"){
    collapsed = force;
    panel.classList.toggle("collapsed", collapsed);
  }else{
    panel.classList.toggle("collapsed");
    collapsed = panel.classList.contains("collapsed");
  }
  btn.textContent = collapsed ? "Mostrar" : "Ocultar";
  btn.setAttribute("aria-expanded", collapsed ? "false" : "true");
}

function renderModelDetails(payload){
  const panel = document.getElementById("model_details_panel");
  if(!panel) return;
  const m  = payload.MODEL;
  const mp = payload.MODEL_PCHIP;
  const rs = payload.RESUMEN||{};
  const dr = payload.DAILY_RATE;

  if(!m){ panel.innerHTML =
    `<div style="color:var(--muted);font-size:11px;padding:8px 0">Sin modelo — subset vacío o insuficiente.</div>`;
    return;
  }

  const isFormula = m.SOURCE === "formula_ganadora";
  const srcColor  = isFormula ? "var(--a1)" : "var(--g1)";
  const srcLabel  = isFormula
    ? `✦ Fórmula ganadora &mdash; ${(m.MODEL_TYPE||"").toUpperCase()}`
    : "◈ PCHIP — Hermite cúbico monotónico";

  // ── KV helper
  const kv = (k,v,cls="")=>
    `<div class="model-kv"><span class="model-kv-key">${k}</span>
     <span class="model-kv-val ${cls}">${v??'—'}</span></div>`;

  // ── HEADER
  let html = `<div class="model-section">
    <div class="model-section-label">Fuente del modelo</div>
    <div style="font-size:12px;font-weight:700;color:${srcColor};padding:2px 0">${srcLabel}</div>
  </div>`;

  // ── FÓRMULA (KaTeX) — placeholder div
  const latexStr = modelFormToKatex(m.MODEL_FORMULA, m.MODEL_TYPE);
  html += `<div class="model-section">
    <div class="model-section-label">Fórmula matemática</div>
    <div class="formula-box" id="katex_formula_main">
      <span style="color:var(--muted);font-size:11px">${m.MODEL_FORMULA||"—"}</span>
    </div>
  </div>`;

  // ── MÉTRICAS
  html += `<div class="model-section">
    <div class="model-section-label">Métricas de ajuste</div>
    ${kv("R²",        m.R2   !=null ? Number(m.R2).toFixed(5)   : "—", "green")}
    ${kv("RMSE",      m.RMSE !=null ? Number(m.RMSE).toFixed(5) : "—", "amber")}
    ${kv("Edad rango",`${m.EDAD_MIN!=null?m.EDAD_MIN.toFixed(0):"?"} – ${m.EDAD_MAX!=null?m.EDAD_MAX.toFixed(0):"?"} días`)}
    ${kv("IM rango",  `${m.IM_MIN!=null?m.IM_MIN.toFixed(4):"?"} – ${m.IM_MAX!=null?m.IM_MAX.toFixed(4):"?"}`)}
  </div>`;

  // ── TASA DE CAMBIO DIARIA
  if(dr != null){
    const drAbs = Math.abs(dr);
    const drSign = dr >= 0 ? "+" : "−";
    const isDown = dr < 0;
    html += `<div class="model-section">
      <div class="model-section-label">Tasa de cambio de IM</div>
      <div style="display:flex;align-items:flex-end;gap:10px;flex-wrap:wrap">
        <div class="rate-chip ${isDown?'down':''}">
          <span class="rate-chip-icon">${isDown?"📉":"📈"}</span>
          <span class="rate-chip-val ${isDown?'down':''}">${drSign}${drAbs.toFixed(5)}</span>
          <span class="rate-chip-label">IM/día (últ. 7 días)</span>
        </div>
        <div style="font-size:10px;color:var(--muted2);line-height:1.4">
          En 7 días: <b style="color:${isDown?'var(--a1)':'var(--g1)'}">${drSign}${(drAbs*7).toFixed(4)} IM</b><br>
          En 30 días: <b style="color:${isDown?'var(--a1)':'var(--g1)'}">${drSign}${(drAbs*30).toFixed(4)} IM</b>
        </div>
      </div>
      <div id="katex_rate" style="margin-top:6px"></div>
    </div>`;
  }

  // ── PCHIP reference (si modelo principal es fórmula)
  if(mp && isFormula){
    html += `<div class="model-section">
      <div class="model-section-label">PCHIP — referencia visual</div>
      ${kv("R²",   mp.R2   !=null ? Number(mp.R2).toFixed(5)   : "—")}
      ${kv("RMSE", mp.RMSE !=null ? Number(mp.RMSE).toFixed(5) : "—")}
    </div>`;
  }

  // ── RESUMEN PANEL
  const rkeys = ["MODEL_USED","MODEL_TYPE","R2","RMSE","IM_NOW","EDAD_DIAS_FIN","FECHA_PANEL_FIN","N"];
  const rRows = rkeys.filter(k=>rs[k]!=null).map(k=>kv(k, rs[k])).join("");
  if(rRows){
    html += `<div class="model-section">
      <div class="model-section-label">Panel resumen (histórico)</div>
      ${rRows}
    </div>`;
  }

  panel.innerHTML = html;

  // ── Render KaTeX after DOM insertion
  if(latexStr){
    const el = document.getElementById("katex_formula_main");
    if(el) renderKatex(el, latexStr);
  }
  // Daily rate KaTeX formula: dIM/dt
  const rateEl = document.getElementById("katex_rate");
  if(rateEl && dr!=null){
    const sign = dr>=0?"+":"-";
    renderKatex(rateEl, `\frac{d\,\mathrm{IM}}{dt} \approx ${sign}${Math.abs(dr).toFixed(5)} \;\mathrm{IM/día}`);
  }
}


// =============================================
// TABS
// =============================================
let ACTIVE_TAB = "tab_curva";

function switchTab(tabId){
  document.querySelectorAll(".tab-btn").forEach(b=>{
    b.classList.toggle("active", b.dataset.tab === tabId);
  });
  document.querySelectorAll(".tab-panel").forEach(p=>{
    p.classList.toggle("active", p.id === tabId);
  });
  ACTIVE_TAB = tabId;
  // Lazy-load tab content
  if(tabId === "tab_prio"    && LAST_APPLY) loadPriorizacion();
  if(tabId === "tab_alertas" && LAST_APPLY) loadAlertas();
  if(tabId === "tab_simulador") { /* user clicks Simular */ }
  if(tabId === "tab_tiff") { /* lazy load TIFF options */ }
}

document.querySelectorAll(".tab-btn").forEach(b=>{
  b.addEventListener("click", ()=>switchTab(b.dataset.tab));
});

// =============================================
// PRIORIZACIÓN TABLE
// =============================================
let PRIO_DATA   = [];
let PRIO_SORT   = {col:"PRIORIDAD", asc:true};
let PRIO_SEM    = "ALL";

const SEM_ROW_CLASS = {
  "red":"prio-row-urgent","orange":"prio-row-warn",
  "purple":"prio-row-urgent","green":"prio-row-ok",
  "teal":"prio-row-ok","yellow":"prio-row-dev","gray":"prio-row-gray"
};

async function loadPriorizacion(){
  const tbody = document.getElementById("prio_tbody");
  tbody.innerHTML = `<tr><td colspan="12" style="text-align:center;padding:24px;color:var(--muted)">
    Cargando tabla de priorización <span class="dots"><span></span><span></span><span></span></span>
  </td></tr>`;
  try{
    const resp = await fetch("/api/tabla_priorizacion",{
      method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({filters: LAST_APPLY ? (LAST_APPLY.filters_applied||{}) : {}})
    });
    const data = await resp.json();
    PRIO_DATA = data.tabla || [];
    renderPrioTable();
  }catch(e){
    tbody.innerHTML = `<tr><td colspan="12" style="color:#ff9090;padding:16px">Error: ${e.message}</td></tr>`;
  }
}

function fmtPct(v){ return v==null ? "—" : (v*100).toFixed(1)+"%"; }
function fmtD(v){ return v==null ? "—" : v+" d"; }
function fmtDt(s){ if(!s) return "—"; try{ const p=s.split("-"); return `${p[2]}/${p[1]}`; }catch(e){return s;} }

function renderPrioTable(){
  const q   = (document.getElementById("prio_search").value||"").toLowerCase();
  let rows  = PRIO_DATA.filter(r=>{
    if(PRIO_SEM !== "ALL" && r.SEMAFORO.color !== PRIO_SEM) return false;
    if(q){
      const s = `${r.PANTE1} ${r.FINCA} ${r.VARIEDAD} ${r.ESTRATO}`.toLowerCase();
      if(!s.includes(q)) return false;
    }
    return true;
  });
  // Sort
  rows.sort((a,b)=>{
    const va = a[PRIO_SORT.col], vb = b[PRIO_SORT.col];
    if(va==null && vb==null) return 0;
    if(va==null) return 1; if(vb==null) return -1;
    const c = (typeof va==="number") ? va-vb : String(va).localeCompare(String(vb));
    return PRIO_SORT.asc ? c : -c;
  });
  document.getElementById("prio_count").textContent = rows.length;

  // Update sort arrows
  document.querySelectorAll(".prio-table th").forEach(th=>{
    th.classList.remove("sorted");
    const arrow = th.querySelector(".sort-arrow");
    if(arrow) arrow.textContent = "↕";
  });

  const tbody = document.getElementById("prio_tbody");
  if(!rows.length){
    tbody.innerHTML = `<tr><td colspan="12" style="text-align:center;color:var(--muted);padding:24px">Sin resultados para el filtro actual.</td></tr>`;
    return;
  }

  tbody.innerHTML = rows.map((r,i)=>{
    const sc  = SEM_ROW_CLASS[r.SEMAFORO.color] || "prio-row-gray";
    const sem = r.SEMAFORO;
    const semCls = "sem-" + sem.color;

    // Tasa con signo y color
    const tasa = r.TASA_DIARIA;
    let tasaHtml = "—";
    if(tasa!=null){
      const sign = tasa>=0?"+":"";
      const cls  = tasa>=0?"sem-teal":"sem-orange";
      tasaHtml = `<span class="${cls}">${sign}${tasa.toFixed(5)}</span>`;
    }
    // Días a cosecha
    let dCosHtml = "—";
    if(r.DIAS_A_COSECHA!=null){
      const d=r.DIAS_A_COSECHA;
      const cls2 = d<0?"sem-red":d<=14?"sem-orange":d<=30?"sem-yellow":"sem-teal";
      dCosHtml = `<span class="${cls2}">${d<0?"vencido "+Math.abs(d)+"d":d+" d"}</span>`;
    }
    // Días a 0.35 / 0.40
    const d35Html = r.DIAS_A_035!=null ? `${r.DIAS_A_035}d<br><span style="font-size:9px;color:var(--muted2)">${fmtDt(r.FECHA_EST_035)}</span>` : "—";
    const d40Html = r.DIAS_A_040!=null ? `${r.DIAS_A_040}d<br><span style="font-size:9px;color:var(--muted2)">${fmtDt(r.FECHA_EST_040)}</span>` : "—";

    // IM con color
    const imV = r.IM_ACTUAL;
    const imCls = imV==null?"sem-gray":imV>=0.40?"sem-green":imV>=0.33?"sem-teal":imV>=0.28?"sem-yellow":"sem-gray";
    const imHtml = imV!=null ? `<span class="${imCls}">${(imV*100).toFixed(1)}%</span>` : "—";

    return `<tr class="${sc}" onclick="selectPanteFromTable('${r.PANTE1}')" style="cursor:pointer" title="Click para ver curva de ${r.PANTE1}">
      <td class="prio-num" style="color:var(--muted2);font-size:10px">${i+1}</td>
      <td><span class="${semCls}">${sem.label}</span></td>
      <td><b>${r.PANTE1}</b></td>
      <td style="color:var(--muted)">${r.FINCA||"—"}</td>
      <td class="prio-num">${imHtml}</td>
      <td class="prio-num">${tasaHtml}</td>
      <td class="prio-num">${dCosHtml}</td>
      <td>${r.FECHACOS_PROY ? fmtDt(r.FECHACOS_PROY.substr ? r.FECHACOS_PROY.substr(0,10)+"z".slice(-1,"z".length-1)||r.FECHACOS_PROY : r.FECHACOS_PROY) : "—"}</td>
      <td class="prio-num">${d35Html}</td>
      <td class="prio-num">${d40Html}</td>
      <td style="color:var(--muted2);font-size:10px">${r.VARIEDAD||"—"}</td>
      <td style="color:var(--muted2);font-size:10px">${r.ESTRATO||"—"}</td>
    </tr>`;
  }).join("");
}

function sortPrio(col){
  if(PRIO_SORT.col === col) PRIO_SORT.asc = !PRIO_SORT.asc;
  else { PRIO_SORT.col = col; PRIO_SORT.asc = true; }
  renderPrioTable();
}

function filterPrioTable(){ renderPrioTable(); }

function setPrioSemFilter(el){
  document.querySelectorAll(".prio-filter-chip").forEach(c=>c.classList.remove("active"));
  el.classList.add("active");
  PRIO_SEM = el.dataset.sem || "ALL";
  renderPrioTable();
}

function selectPanteFromTable(pante1){
  // Select in f_pante1 dropdown and apply filters → switch to curva tab
  const sel = document.getElementById("f_pante1");
  if([...sel.options].some(o=>o.value===pante1)){
    sel.value = pante1;
    switchTab("tab_curva");
    applyFilters();
  }
}

function exportPrioCSV(){
  if(!PRIO_DATA.length) return;
  const cols = ["PRIORIDAD","SEMAFORO_LABEL","PANTE1","FINCA","VARIEDAD","ESTRATO",
                 "IM_ACTUAL","TASA_DIARIA","DIAS_A_COSECHA","FECHACOS_PROY",
                 "SEMANA_PROY","TERCIO_PROY","DIAS_A_035","FECHA_EST_035",
                 "DIAS_A_040","FECHA_EST_040"];
  const rows = [cols.join(",")];
  PRIO_DATA.forEach(r=>{
    rows.push([
      r.PRIORIDAD, `"${r.SEMAFORO?.label||""}"`, r.PANTE1,
      `"${r.FINCA||""}"`, `"${r.VARIEDAD||""}"`, `"${r.ESTRATO||""}"`,
      r.IM_ACTUAL!=null?r.IM_ACTUAL.toFixed(5):"",
      r.TASA_DIARIA!=null?r.TASA_DIARIA.toFixed(6):"",
      r.DIAS_A_COSECHA!= null?r.DIAS_A_COSECHA:"",
      r.FECHACOS_PROY||"", r.SEMANA_PROY||"", r.TERCIO_PROY||"",
      r.DIAS_A_035!=null?r.DIAS_A_035:"", r.FECHA_EST_035||"",
      r.DIAS_A_040!=null?r.DIAS_A_040:"", r.FECHA_EST_040||"",
    ].join(","));
  });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([rows.join("\n")],{type:"text/csv"}));
  a.download = `priorizacion_IM_${new Date().toISOString().slice(0,10)}.csv`;
  a.click();
}

// =============================================
// ALERTAS
// =============================================
const ALERT_BG = {
  red:"rgba(255,107,107,.10)",orange:"rgba(255,123,84,.10)",
  yellow:"rgba(255,229,102,.08)",green:"rgba(78,203,113,.08)",
  teal:"rgba(0,229,176,.07)",purple:"rgba(192,132,252,.08)",gray:"rgba(255,255,255,.04)"
};
const ALERT_BORDER = {
  red:"rgba(255,107,107,.30)",orange:"rgba(255,123,84,.25)",
  yellow:"rgba(255,229,102,.25)",green:"rgba(78,203,113,.25)",
  teal:"rgba(0,229,176,.25)",purple:"rgba(192,132,252,.25)",gray:"rgba(255,255,255,.10)"
};

async function loadAlertas(){
  document.getElementById("alertas_body").innerHTML =
    `<div style="color:var(--muted);font-size:11px;padding:8px 0">
       Cargando alertas <span class="dots"><span></span><span></span><span></span></span>
     </div>`;
  try{
    const resp = await fetch("/api/alertas",{
      method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({filters: LAST_APPLY ? (LAST_APPLY.filters_applied||{}) : {}})
    });
    const data = await resp.json();
    renderAlertas(data);
  }catch(e){
    document.getElementById("alertas_body").innerHTML =
      `<div style="color:#ff9090">Error: ${e.message}</div>`;
  }
}

function renderAlertas(data){
  const rs = data.resumen || {};
  // Resumen chips
  const chips = [
    {n:rs.criticos||0,    label:"Críticos",     cls:"sem-red"},
    {n:rs.advertencia||0, label:"Advertencia",  cls:"sem-orange"},
    {n:rs.listos||0,      label:"Listos",        cls:"sem-green"},
    {n:rs.en_desarrollo||0,label:"En desarrollo",cls:"sem-yellow"},
    {n:rs.total_pantes||0,label:"Total pantes",  cls:""},
  ];
  document.getElementById("alertas_resumen").innerHTML = chips.map(c=>`
    <div class="resumen-chip">
      <div class="resumen-chip-n ${c.cls}">${c.n}</div>
      <div class="resumen-chip-label">${c.label}</div>
    </div>`).join("");

  const alertas = data.alertas || [];
  if(!alertas.length){
    document.getElementById("alertas_body").innerHTML =
      `<div style="color:var(--muted);font-size:11px;padding:8px 0">Sin alertas en el filtro actual.</div>`;
    return;
  }

  document.getElementById("alertas_body").innerHTML = alertas.map((g,gi)=>{
    const bg  = ALERT_BG[g.color]  || ALERT_BG.gray;
    const brd = ALERT_BORDER[g.color] || ALERT_BORDER.gray;
    const rows = (g.pantes||[]).map(p=>{
      const tasa = p.TASA_DIARIA;
      let tasaHtml = "—";
      if(tasa!=null){
        const s=tasa>=0?"+":"";
        const cls=tasa>=0?"sem-teal":"sem-orange";
        tasaHtml=`<span class="${cls}">${s}${tasa.toFixed(5)}</span>`;
      }
      const dCos = p.DIAS_A_COSECHA;
      const dCosHtml = dCos!=null
        ? `<span class="${dCos<0?"sem-red":dCos<=14?"sem-orange":"sem-teal"}">${dCos<0?"−"+Math.abs(dCos)+"d":"+"+dCos+"d"}</span>`
        : "—";
      return `<tr onclick="selectPanteFromTable('${p.PANTE1}')" style="cursor:pointer" title="Ver curva">
        <td><b>${p.PANTE1}</b></td>
        <td>${p.FINCA||"—"}</td>
        <td class="prio-num">${p.IM_ACTUAL!=null?p.IM_ACTUAL.toFixed(3):"—"}</td>
        <td class="prio-num">${tasaHtml}</td>
        <td class="prio-num">${dCosHtml}</td>
        <td>${p.FECHACOS_PROY ? p.FECHACOS_PROY.toString().slice(0,10) : "—"}</td>
      </tr>`;
    }).join("");

    return `<div class="alert-group" style="border-color:${brd}">
      <div class="alert-header" style="background:${bg}" onclick="toggleAlertGroup(${gi})">
        <span>${g.label}</span>
        <span class="alert-badge">${(g.pantes||[]).length} pante${(g.pantes||[]).length!==1?"s":""}</span>
      </div>
      <div class="alert-body ${gi===0?"open":""}" id="alertbody_${gi}">
        <table class="alert-mini-table">
          <thead><tr>
            <th>Pante</th><th>Finca</th>
            <th>IM</th><th>Tasa/día</th>
            <th>Días→cos.</th><th>Fecha cos.</th>
          </tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </div>
    </div>`;
  }).join("");
}

function toggleAlertGroup(i){
  const b = document.getElementById("alertbody_"+i);
  if(b) b.classList.toggle("open");
}

// =============================================
// RASTER TIFF (v8/v9)
// =============================================
async function loadTiff(){
  const pante1 = document.getElementById("tiff_pante1").value;
  if(!pante1){
    document.getElementById("tiff_results").innerHTML =
      `<div style="color:#ff9090;padding:12px">⚠️ Selecciona un PANTE1 para cargar el raster</div>`;
    return;
  }
  document.getElementById("tiff_results").innerHTML =
    `<div style="color:var(--muted);font-size:11px;padding:12px">
       🔄 Cargando raster TIFF para ${pante1}...
       <span class="dots"><span></span><span></span><span></span></span>
     </div>`;
  try{
    const resp = await fetch("/api/tiff",{
      method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({pante1: pante1})
    });
    
    const data = await resp.json();
    
    if(!resp.ok || data.error){
      let errorHtml = `<div style="color:#ff9090;padding:12px">
        <div style="font-weight:bold;margin-bottom:8px">❌ Error: ${data.error || 'Error desconocido'}</div>`;
      
      if(data.detail){
        errorHtml += `<div style="font-size:10px;color:var(--muted);margin-bottom:6px">${data.detail}</div>`;
      }
      
      if(data.available_sample){
        errorHtml += `<div style="font-size:10px;color:var(--muted);margin-top:8px">
          <b>Pantes disponibles (muestra):</b><br>
          ${data.available_sample.slice(0,5).join(', ')}...
        </div>`;
      }
      
      if(data.searched_names){
        errorHtml += `<div style="font-size:10px;color:var(--muted);margin-top:8px">
          <b>Archivos buscados:</b><br>
          ${data.searched_names.join('<br>')}
        </div>`;
      }
      
      errorHtml += `</div>`;
      document.getElementById("tiff_results").innerHTML = errorHtml;
      return;
    }
    
    // Renderizar resultado exitoso
    let resultHtml = `<div style="padding:12px">
      <div style="color:var(--g1);font-weight:bold;margin-bottom:12px">
        ✅ Raster procesado exitosamente
      </div>
      <div style="font-size:11px;line-height:1.8">
        <div><b>PANTE1:</b> ${data.pante1}</div>
        <div><b>Archivo:</b> ${data.tiff_file || 'N/A'}</div>`;
    
    if(data.shape){
      resultHtml += `<div><b>Dimensiones:</b> ${data.shape.width} × ${data.shape.height} px (${data.shape.bands} banda${data.shape.bands>1?'s':''})</div>`;
    }
    
    if(data.statistics){
      const s = data.statistics;
      resultHtml += `<div style="margin-top:8px;padding-top:8px;border-top:1px solid rgba(255,255,255,.1)">
        <b>Estadísticas:</b><br>
        Píxeles válidos: ${s.pixels?.toLocaleString() || 'N/A'}<br>
        Min: ${s.min != null ? s.min.toFixed(4) : 'N/A'}<br>
        Max: ${s.max != null ? s.max.toFixed(4) : 'N/A'}<br>
        Promedio: ${s.mean != null ? s.mean.toFixed(4) : 'N/A'}
      </div>`;
    }
    
    resultHtml += `</div></div>`;
    document.getElementById("tiff_results").innerHTML = resultHtml;
    
  }catch(e){
    document.getElementById("tiff_results").innerHTML =
      `<div style="color:#ff9090;padding:12px">
        <b>❌ Error de conexión:</b><br>
        <div style="font-size:10px;margin-top:6px">${e.message}</div>
      </div>`;
  }
}

document.getElementById("btn_load_tiff").addEventListener("click", loadTiff);


// =============================================
// SIMULADOR DE COSECHA
// =============================================
let SIM_DATA = [];

async function runSimulador(){
  const im_target = parseFloat(document.getElementById("sim_im_target").value);
  if(!isFinite(im_target) || im_target<=0 || im_target>1){
    document.getElementById("sim_results").innerHTML =
      `<div style="color:#ff9090">Ingresa un IM válido (0.01 – 0.99)</div>`; return;
  }
  document.getElementById("sim_results").innerHTML =
    `<div style="color:var(--muted);padding:16px;text-align:center">
      Calculando curvas para todos los pantes del filtro activo…
      <span class="dots"><span></span><span></span><span></span></span>
    </div>`;
  document.getElementById("btn_sim_export").style.display = "none";
  try{
    const resp = await fetch("/api/simulador_im",{
      method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({
        filters: LAST_APPLY ? (LAST_APPLY.filters_applied||{}) : {},
        im_target: im_target
      })
    });
    const data = await resp.json();
    SIM_DATA = data.rows || [];
    renderSimulador(im_target, SIM_DATA);
  }catch(e){
    document.getElementById("sim_results").innerHTML =
      `<div style="color:#ff9090">Error: ${e.message}</div>`;
  }
}

function renderSimulador(im_target, rows){
  if(!rows.length){
    document.getElementById("sim_results").innerHTML =
      `<div style="color:var(--muted);padding:16px">
        Ningún pante del filtro activo alcanza IM = ${im_target.toFixed(2)} según el modelo.
      </div>`; return;
  }
  document.getElementById("btn_sim_export").style.display = "";

  const maxDias = Math.max(...rows.map(r=>r.DIAS_FALTAN||0), 1);

  // Agrupar por semana calendar
  const byWeek = {};
  rows.forEach(r=>{
    const w = r.FECHA_ESTIMADA ? r.FECHA_ESTIMADA.slice(0,7) : "Sin fecha";
    if(!byWeek[w]) byWeek[w] = [];
    byWeek[w].push(r);
  });

  const weeks = Object.keys(byWeek).sort();
  let html = `
    <div style="font-size:10px;color:var(--muted2);padding:4px 0 10px">
      <b style="color:var(--g1)">${rows.length} pantes</b> alcanzarán 
      <b style="color:var(--a1)">IM = ${(im_target*100).toFixed(1)}%</b> · 
      desde ${rows[0]?.DIAS_FALTAN}d hasta ${rows[rows.length-1]?.DIAS_FALTAN}d
    </div>`;

  weeks.forEach(w=>{
    const wpantes = byWeek[w];
    const label = w==="Sin fecha" ? "Sin fecha estimada" : (() => {
      try{ const d=new Date(w+"-01"); return d.toLocaleDateString("es-GT",{month:"long",year:"numeric"}); }
      catch(e){ return w; }
    })();
    html += `<div class="sim-result-card">
      <div class="sim-result-hd">
        <span>📅 ${label} — <b>${wpantes.length} pante${wpantes.length!==1?"s":""}</b></span>
      </div>`;
    wpantes.forEach(r=>{
      const pct = Math.round((r.DIAS_FALTAN||0)/maxDias*100);
      const dCos = r.DELTA_DIAS;
      let deltaHtml = "";
      if(dCos!=null){
        const absd=Math.abs(dCos);
        if(dCos<-1)      deltaHtml = `<span class="sem-orange">⬆ ${absd}d antes de programada</span>`;
        else if(dCos>1)  deltaHtml = `<span class="sem-red">⬇ ${absd}d después de programada</span>`;
        else             deltaHtml = `<span class="sem-teal">✓ Coincide con programación</span>`;
      } else if(r.FECHACOS_PROY) {
        deltaHtml = `<span style="color:var(--muted2)">Cos. prog: ${r.FECHACOS_PROY.toString().slice(0,10)}</span>`;
      }
      html += `
        <div style="display:flex;align-items:center;gap:10px;padding:7px 12px;
          border-bottom:1px solid rgba(255,255,255,.04);cursor:pointer"
          onclick="selectPanteFromTable('${r.PANTE1}')" title="Ver curva de ${r.PANTE1}">
          <div style="min-width:90px">
            <b style="font-size:12px">${r.PANTE1}</b>
            <div style="font-size:9px;color:var(--muted2)">${r.FINCA||""}</div>
          </div>
          <div style="min-width:60px;text-align:right;font-family:var(--font-head);font-weight:700;color:var(--a1)">
            ${r.DIAS_FALTAN}d
          </div>
          <div class="sim-bar-wrap">
            <div class="sim-bar" style="width:${pct}%"></div>
          </div>
          <div style="min-width:70px;font-size:10px;text-align:right">
            ${r.FECHA_ESTIMADA ? r.FECHA_ESTIMADA.slice(5) : "—"}
          </div>
          <div style="min-width:150px;font-size:10px;text-align:right">
            ${deltaHtml}
          </div>
          <div style="min-width:55px;font-size:10px;text-align:right;color:var(--muted2)">
            IM ${r.IM_ACTUAL!=null?r.IM_ACTUAL.toFixed(3):"—"}
          </div>
        </div>`;
    });
    html += `</div>`;
  });

  document.getElementById("sim_results").innerHTML = html;
}

function exportSimCSV(){
  if(!SIM_DATA.length) return;
  const cols = ["PANTE1","FINCA","VARIEDAD","IM_ACTUAL","DIAS_FALTAN",
                 "FECHA_ESTIMADA","FECHACOS_PROY","DELTA_DIAS"];
  const rows = [cols.join(",")];
  SIM_DATA.forEach(r=>rows.push([
    r.PANTE1,`"${r.FINCA||""}"`,`"${r.VARIEDAD||""}"`,
    r.IM_ACTUAL!=null?r.IM_ACTUAL.toFixed(5):"",
    r.DIAS_FALTAN!=null?r.DIAS_FALTAN:"",
    r.FECHA_ESTIMADA||"", r.FECHACOS_PROY||"",
    r.DELTA_DIAS!=null?r.DELTA_DIAS:""
  ].join(",")));
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([rows.join("\n")],{type:"text/csv"}));
  a.download = `simulador_IM_${new Date().toISOString().slice(0,10)}.csv`;
  a.click();
}

// Reload tables when filters are applied
const _origApplyFilters = applyFilters;
applyFilters = async function(){
  await _origApplyFilters.apply(this, arguments);
  // Refresh active tab if it's one of the new tabs
  if(ACTIVE_TAB === "tab_prio")    loadPriorizacion();
  if(ACTIVE_TAB === "tab_alertas") loadAlertas();
};


// =============================================
// EVENT LISTENERS
// =============================================
document.getElementById("btn_apply").addEventListener("click", applyFilters);
document.getElementById("btn_reset").addEventListener("click", resetAll);
document.getElementById("btn_calc_im").addEventListener("click", calcDaysFromIM);
document.getElementById("btn_calc_fecha").addEventListener("click", calcIMFromDate);
document.getElementById("btn_csv").addEventListener("click", exportCSV);
document.getElementById("btn_png").addEventListener("click", exportPNG);
document.getElementById("btn_sim").addEventListener("click", runSimulador);
["f_cosnxs","f_region","f_finca","f_varie","f_estrato","f_pante1","f_semana","f_tercio"].forEach(id=>{
  document.getElementById(id).addEventListener("change", refreshOptionsCascade);
});

// =============================================
// BOOTSTRAP
// =============================================
(async function bootstrap(){
  setStatus(true,"Verificando estado del sistema...");

  try{
    // Primero verificar status del warm up
    const statusResp = await fetch("/api/status");
    const status = await statusResp.json();

    if(status.error || !status.warm_up_success){
      setStatus(false,"Error en inicialización");
      setCalcStatus(`<span style="color:#ff9090">Error en warm up: ${status.error || 'Datos no cargados correctamente'}</span>`);
      return;
    }

    // Mostrar resumen de datos cargados
    const dataSummary = `
      <div style="font-size:10px;color:var(--muted2);margin-bottom:8px">
        📊 Diario: ${status.panel_diario_rows} regs, ${status.pantes_activos_count} pantes<br>
        📈 Resumen: ${status.panel_resumen_rows} filas<br>
        ⚖️ Balance: ${status.panel_balance_rows} pantes
      </div>
    `;

    setStatus(true,"Cargando opciones de filtros...");
    const resp = await fetch("/api/bootstrap",{method:"POST"});
    const data = await resp.json();
    if(data.error){
      setStatus(false,"Error");
      setCalcStatus(`<span style="color:#ff9090">${data.error}</span>`);
      return;
    }

    OPTIONS = data.options||{};
    applySelectMaps();
    updateUI(data.payload);
    setStatus(false,"Sistema listo");
    setCalcStatus(`${dataSummary}Vista general activa. Filtra y presiona <b>Aplicar</b>.`);
    // Precarga automática de curva IM (v8)
    applyFilters();
  }catch(e){
    setStatus(false,"Error de conexión");
    setCalcStatus(`<span style="color:#ff9090">Error: ${e.message}</span>`);
  }
})();
</script>
</body>
</html>

"""

if __name__ == "__main__":
    import argparse, os, webbrowser, threading

    parser = argparse.ArgumentParser(
        description="IM Dashboard v9 — Proyección de Índice de Maduración"
    )
    parser.add_argument("--host",  default=os.getenv("IM_HOST",  HOST_IP),
                        help=f"Host (default {HOST_IP}). Usa 0.0.0.0 para red local.")
    parser.add_argument("--port",  default=int(os.getenv("IM_PORT", "5001")),
                        type=int,  help="Puerto (default 5001)")
    parser.add_argument("--debug", action="store_true",
                        default=os.getenv("IM_DEBUG","0") == "1",
                        help="Modo debug Flask (recarga automática)")
    parser.add_argument("--no-browser", action="store_true",
                        help="No abrir navegador automáticamente")
    args = parser.parse_args()

    # ── Resumen de datos cargados ──────────────────────────────────────
    SEP = "─" * 58
    print(f"\n{'🌾  IM Dashboard v9':^58}")
    print(SEP)
    print(f"  {'Panel diario':<26}: {len(PANEL_DIARIO):>8,} registros")
    print(f"  {'Panel resumen':<26}: {len(PANEL_RESUMEN):>8,} filas")
    print(f"  {'Pantes únicos (diario)':<26}: {PANEL_DIARIO['PANTE1'].nunique():>8,}")
    print(f"  {'Pantes activos (no cosechados)':<26}: {len(_PANTES_ACTIVOS):>8,}")

    if not PANEL_BALANCE.empty:
        bal_con_fecha = PANEL_BALANCE["FECHACOS_PROY"].notna().sum()
        fecha_min     = PANEL_BALANCE["FECHACOS_PROY"].min()
        fecha_max     = PANEL_BALANCE["FECHACOS_PROY"].max()
        print(f"  {'Balance cosecha — pantes':<26}: {len(PANEL_BALANCE):>8,}")
        print(f"  {'  con FECHACOS_PROY':<26}: {bal_con_fecha:>8,}")
        print(f"  {'  rango de fechas':<26}: {str(fecha_min)[:10]} → {str(fecha_max)[:10]}")
    else:
        print(f"  {'Balance cosecha':<26}: {'NO CARGADO':>8}")
        print(f"  {'  → Exporta la consulta SQL':<26}")
        print(f"  {'  → a balance_cosecha_2526.csv':<26}")

    url = f"http://{HOST_IP}:{args.port}" if args.host == "0.0.0.0" else f"http://{args.host}:{args.port}"
    lan_url = f"http://{HOST_IP}:{args.port}" if args.host == "0.0.0.0" else None
    print(SEP)
    print(f"  🌐  Servidor : {url}")
    if lan_url:
        print(f"  🌐  LAN URL  : {lan_url}")
    print(f"  🐛  Debug    : {'ON' if args.debug else 'OFF'}")
    print(SEP + "\n")

    # ── Abrir navegador automáticamente ───────────────────────────────
    if not args.no_browser and not args.debug:
        def _open():
            import time; time.sleep(1.2)
            webbrowser.open(url)
        threading.Thread(target=_open, daemon=True).start()

    app.run(host=args.host, port=args.port, debug=args.debug, use_reloader=args.debug)

