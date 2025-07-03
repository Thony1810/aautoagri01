# Requiere: streamlit, pandas, plotly, base64
import streamlit as st
import pandas as pd
import os
import plotly.express as px
import base64
from sklearn.tree import DecisionTreeClassifier, plot_tree
from sklearn.preprocessing import LabelEncoder
from sklearn.ensemble import RandomForestClassifier

st.set_page_config(layout="wide")

# =================== ESTILO PERSONALIZADO =====================
st.markdown("""
    <style>
        body, .stApp {
            background-color: #111111;
            color: #e0e0e0;
        }
        .stSidebar {
            background-color: #111111 !important;
        }
        .stSelectbox, .stMultiSelect, .stTextInput, .stNumberInput, .stButton, .stDownloadButton {
            background-color: #222 !important;
            border: 2px solid #222 !important;
            color: #00cfff !important;
            border-radius: 8px !important;
        }
        .stMultiSelect > div, .stSelectbox > div {
            background-color: #222 !important;
            color: #00cfff !important;
        }
        .st-cb, .st-cb label {
            color: #00cfff !important;
        }
        .st-bb {
            border-color: #222 !important;
        }
        .stPlotlyChart {
            background-color: #111111 !important;
        }
        .css-1v0mbdj, .css-1v0mbdj:focus {
            background-color: #222 !important;
            color: #00cfff !important;
        }
        .stMetric {
            background-color: #222 !important;
            border-radius: 8px;
            border: 1px solid #00cfff;
            color: #00cfff !important;
        }
    </style>
""", unsafe_allow_html=True)

# =================== CONFIGURACIÓN =====================
LOGO_PATH = r"C:/Users/AAUTOAGRI01/OneDrive - Compania Agricola Industrial Santa Ana, S. A/Imágenes/Santa Ana/Logos oficiales/Logos oficiales/logo-fondo-verde.png"
CARPETA_CUMPLE = r"C:\Users\AAUTOAGRI01\OneDrive - Compania Agricola Industrial Santa Ana, S. A\Documentos\FERTILIZACION\RESULTADOS\comparativos PDF PANTE1\CUMPLE"
CARPETA_NOCUMPLE = r"C:\Users\AAUTOAGRI01\OneDrive - Compania Agricola Industrial Santa Ana, S. A\Documentos\FERTILIZACION\RESULTADOS\comparativos PDF PANTE1\NO_CUMPLE"
CSV_PATH = r"C:\\Users\\AAUTOAGRI01\\OneDrive - Compania Agricola Industrial Santa Ana, S. A\\Documentos\\FERTILIZACION\\RESULTADOS\\Resumen_Masivo_PANTE1_{}.csv"

# =================== CARGA DE DATOS =====================
if not os.path.exists(CSV_PATH):
    st.error(f"No se encontró el archivo CSV en: {CSV_PATH}")
    st.stop()

if not os.path.isdir(CARPETA_CUMPLE):
    st.error(f"No se encontró la carpeta de PDFs que CUMPLEN: {CARPETA_CUMPLE}")
    st.stop()

if not os.path.isdir(CARPETA_NOCUMPLE):
    st.error(f"No se encontró la carpeta de PDFs que NO CUMPLEN: {CARPETA_NOCUMPLE}")
    st.stop()

df = pd.read_csv(CSV_PATH)

# =================== LOGO Y TÍTULO =====================
with st.sidebar:
    st.image(LOGO_PATH, width=250)
    st.markdown("<h2 style='color:#00cfff;'>Dashboard de Fertilización</h2>", unsafe_allow_html=True)
    st.markdown("<hr style='border:1px solid #00cfff;'>", unsafe_allow_html=True)

# =================== FILTROS EN CASCADA EN SIDEBAR =====================
with st.sidebar:
    st.markdown(
        """
        <style>
        .stMultiSelect > div, .stSelectbox > div, .stTextInput > div, .stNumberInput > div {
            background-color: #1a2636 !important;
            color: #00cfff !important;
            border-radius: 8px !important;
            border: 1.5px solid #00cfff !important;
        }
        .stMultiSelect label, .stSelectbox label, .stTextInput label, .stNumberInput label {
            color: #00cfff !important;
            font-weight: bold;
        }
        .stMultiSelect [data-baseweb="tag"], .stMultiSelect [data-baseweb="tag"] span {
            background: linear-gradient(90deg, #00cfff 0%, #6ab187 100%) !important;
            color: #111111 !important;
            border-radius: 6px !important;
        }
        .stMultiSelect [data-baseweb="select"] {
            background-color: #1a2636 !important;
        }
        </style>
        """,
        unsafe_allow_html=True
    )

    cumple_global = st.multiselect(
        "Filtrar por Cumplimiento Global",
        options=["CUMPLE", "NO CUMPLE"],
        default=["CUMPLE", "NO CUMPLE"],
        key="cumple_global"
    )
    if set(cumple_global) == {"CUMPLE", "NO CUMPLE"}:
        df_cumple = df.copy()
    elif "CUMPLE" in cumple_global:
        df_cumple = df[df["CATEGORIA_CUMPLIMIENTO"] == "EN RANGO"]
    elif "NO CUMPLE" in cumple_global:
        df_cumple = df[df["CATEGORIA_CUMPLIMIENTO"] != "EN RANGO"]
    else:
        df_cumple = df.iloc[0:0]

    categorias = df_cumple["CATEGORIA_CUMPLIMIENTO"].unique()
    cumplimiento = st.multiselect(
        "Filtrar por CATEGORIA_CUMPLIMIENTO",
        options=categorias,
        default=list(categorias),
        key="cumplimiento"
    )
    df_cat = df_cumple[df_cumple["CATEGORIA_CUMPLIMIENTO"].isin(cumplimiento)]

    regiones = df_cat["REGION"].unique()
    regiones_sel = st.multiselect(
        "Filtrar por REGION",
        options=regiones,
        default=list(regiones),
        key="regiones"
    )
    df_regio = df_cat[df_cat["REGION"].isin(regiones_sel)]

    fincas = df_regio["FINCA"].unique()
    fincas_sel = st.multiselect(
        "Filtrar por FINCA",
        options=fincas,
        default=list(fincas),
        key="fincas"
    )
    df_finca = df_regio[df_regio["FINCA"].isin(fincas_sel)]

    pantes = df_finca["PANTE1"].astype(str).unique()
    pantes_sel = st.multiselect(
        "Filtrar por PANTE1",
        options=pantes,
        default=list(pantes),
        key="pantes"
    )

# DataFrame final filtrado
df_filtrado = df_finca[df_finca["PANTE1"].astype(str).isin(pantes_sel)]

# =================== KPIs =====================
st.markdown(
    """
    <style>
    .kpi-futurista {
        background: linear-gradient(135deg, #0f2027 0%, #2c5364 100%);
        border-radius: 18px;
        box-shadow: 0 4px 24px #00cfff44, 0 1.5px 0 #00cfff inset;
        padding: 28px 0 18px 0;
        margin-bottom: 12px;
        text-align: center;
        transition: box-shadow 0.3s;
        border: 1.5px solid #00cfff55;
        position: relative;
        overflow: hidden;
    }
    .kpi-futurista h3 {
        margin: 0;
        font-size: 2.8rem;
        color: #00fff7;
        letter-spacing: 2px;
        text-shadow: 0 0 8px #00cfff99, 0 0 2px #fff;
        font-family: 'Orbitron', 'Segoe UI', monospace;
    }
    .kpi-futurista .desc {
        color: #b2fefa;
        font-size: 1.1rem;
        margin-top: 8px;
        letter-spacing: 1px;
        font-family: 'Orbitron', 'Segoe UI', monospace;
        opacity: 0.85;
    }
    .kpi-futurista:before {
        content: "";
        position: absolute;
        top: -40px; left: -40px; right: -40px; bottom: -40px;
        background: radial-gradient(circle, #00cfff22 0%, transparent 80%);
        z-index: 0;
    }
    </style>
    <link href="https://fonts.googleapis.com/css?family=Orbitron:700&display=swap" rel="stylesheet">
    """,
    unsafe_allow_html=True
)

st.title("Dashboard de Fertilización - Comparativo Ejecutado vs Diseñado")
kpi1, kpi2, kpi3 = st.columns(3)
with kpi1:
    st.markdown(
        f"""
        <div class="kpi-futurista">
            <h3>{round(df_filtrado['DOSIS_EJECUTADA'].mean(), 2)}</h3>
            <div class="desc">Dosis Prom. Ejecutada (kg/ha)</div>
        </div>
        """, unsafe_allow_html=True
    )
with kpi2:
    st.markdown(
        f"""
        <div class="kpi-futurista">
            <h3>{round(df_filtrado['DOSIS_DISEÑO'].mean(), 2)}</h3>
            <div class="desc">Dosis Prom. Diseñada (kg/ha)</div>
        </div>
        """, unsafe_allow_html=True
    )
with kpi3:
    st.markdown(
        f"""
        <div class="kpi-futurista">
            <h3>{round(100 - df_filtrado['DIFERENCIA_PCT'].abs().mean(), 1)}%</h3>
            <div class="desc">Diferencia Porcentual</div>
        </div>
        """, unsafe_allow_html=True
    )

kpi4, kpi5, kpi6 = st.columns(3)
with kpi4:
    st.markdown(
        f"""
        <div class="kpi-futurista">
            <h3>{df_filtrado['PANTE1'].nunique()}</h3>
            <div class="desc">Recuento PANTE1 Diseñados</div>
        </div>
        """, unsafe_allow_html=True
    )
with kpi5:
    st.markdown(
        f"""
        <div class="kpi-futurista">
            <h3>{df_filtrado[~df_filtrado['DOSIS_EJECUTADA'].isna()]['PANTE1'].nunique()}</h3>
            <div class="desc">Recuento PANTE1 Ejecutados</div>
        </div>
        """, unsafe_allow_html=True
    )
with kpi6:
    porcentaje_cumplimiento = (
        df_filtrado['DOSIS_EJECUTADA'].sum() / df_filtrado['DOSIS_DISEÑO'].sum() * 100
        if df_filtrado['DOSIS_DISEÑO'].sum() > 0 else 0
    )
    st.markdown(
        f"""
        <div class="kpi-futurista">
            <h3>{round(porcentaje_cumplimiento, 1)}%</h3>
            <div class="desc">% Cumplimiento (Ejecutado/Diseño)</div>
        </div>
        """, unsafe_allow_html=True
    )

# =================== GRÁFICOS =====================
st.subheader("Gráficos de Análisis")
col4, col5 = st.columns(2)
with col4:
    fig1 = px.histogram(
        df_filtrado, x="CATEGORIA_CUMPLIMIENTO", color="CATEGORIA_CUMPLIMIENTO",
        title="Distribución de Categorías de Cumplimiento",
        color_discrete_sequence=["#ffff00", "#FF0000", "#04FF00", "#0077b6"]
    )
    fig1.update_layout(plot_bgcolor='#111111', paper_bgcolor='#111111', font_color='#00cfff')
    st.plotly_chart(fig1, use_container_width=True)
with col5:
    fig2 = px.pie(
        df_filtrado, names="CATEGORIA_CUMPLIMIENTO", values="AREA_CAT_CUMPLIMIENTO",
        title="Distribución del Área por Categoría",
        color_discrete_sequence=["#ffff00", "#FF0000", "#04FF00", "#0077b6"]
    )
    fig2.update_layout(plot_bgcolor='#111111', paper_bgcolor='#111111', font_color='#00cfff')
    st.plotly_chart(fig2, use_container_width=True)


# =================== VISOR DE PDF MEJORADO =====================
st.subheader("Visor de PDF por PANTE1")

def buscar_pdfs_filtrados(pantes, folders, cumple_status):
    pdfs = {}
    for pante in pantes:
        encontrados = []
        for folder in folders:
            for root, dirs, files in os.walk(folder):
                for file in files:
                    if file.lower().endswith(".pdf") and str(pante) in file:
                        if cumple_status == "TODOS":
                            encontrados.append(os.path.join(root, file))
                        elif cumple_status == "EN RANGO" and folder == CARPETA_CUMPLE:
                            encontrados.append(os.path.join(root, file))
                        elif cumple_status == "NO CUMPLE (SUBDOSIFICADO o SOBREDOSIFICADO)" and folder == CARPETA_NOCUMPLE:
                            encontrados.append(os.path.join(root, file))
        if encontrados:
            pdfs[pante] = encontrados
    return pdfs

cumple_status = "TODOS"
folders_to_search = [CARPETA_CUMPLE, CARPETA_NOCUMPLE]
pdfs_disponibles = buscar_pdfs_filtrados(pantes_sel, folders_to_search, cumple_status)

# Limitar la visualización a un máximo de 10 mapas
max_mapas = 10
pantes_a_mostrar = list(pdfs_disponibles.keys())[:max_mapas]

if not pantes_a_mostrar:
    st.warning("No hay PDFs disponibles para los filtros seleccionados.")
else:
    if len(pdfs_disponibles) > max_mapas:
        st.info(f"Mostrando solo los primeros {max_mapas} mapas de {len(pdfs_disponibles)} disponibles.")
    for pante in pantes_a_mostrar:
        archivos = pdfs_disponibles[pante]
        st.markdown(f"<h4 style='color:#00cfff;'>PANTE1: {pante}</h4>", unsafe_allow_html=True)
        if len(archivos) > 1:
            st.info(f"Se encontraron varios PDFs para este PANTE1. Mostrando el primero: {os.path.basename(archivos[0])}")
        else:
            st.markdown(f"<b style='color:#00cfff;'>Archivo PDF encontrado:</b> {os.path.basename(archivos[0])}", unsafe_allow_html=True)

        pdf_path = archivos[0]
        with open(pdf_path, "rb") as f:
            base64_pdf = base64.b64encode(f.read()).decode('utf-8')
            pdf_display = f'<iframe src="data:application/pdf;base64,{base64_pdf}" width="100%" height="900" type="application/pdf"></iframe>'
            st.markdown(pdf_display, unsafe_allow_html=True)
            st.download_button("Descargar PDF", data=base64.b64decode(base64_pdf), file_name=os.path.basename(pdf_path))
