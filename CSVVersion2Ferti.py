# ==================== IMPORTACIONES ====================
import pandas as pd
import geopandas as gpd
import numpy as np
import os
import matplotlib.pyplot as plt
from shapely.geometry import Polygon
from shapely import affinity
from matplotlib.patches import Rectangle
from matplotlib.colors import ListedColormap
from matplotlib.backends.backend_pdf import PdfPages
from scipy.spatial import cKDTree
from itertools import cycle, islice
import matplotlib.image as mpimg
import matplotlib as mpl
from matplotlib.colors import BoundaryNorm


# ==================== RUTAS ====================
shp_diseno_path2 = r"C:\Users\AAUTOAGRI01\OneDrive - Compania Agricola Industrial Santa Ana, S. A\Archivos de Geraldine Melina Giron Rodas - ZAFRA 24-25\SHAPES DE DOSIS FERTILIZACION\2DA ENTREGA 20250506\BLOQUES DE APLICACION 20250506.shp"
shp_diseno_path = r"C:\Users\AAUTOAGRI01\OneDrive - Compania Agricola Industrial Santa Ana, S. A\Archivos de Geraldine Melina Giron Rodas - ZAFRA 24-25\SHAPES DE DOSIS FERTILIZACION\1RA ENTREGA 20250505\BLOQUES DE APLICACION 20250505.shp"
csv_path = r"C:\Users\AAUTOAGRI01\OneDrive - Compania Agricola Industrial Santa Ana, S. A\Documentos\FERTILIZACION\RESULTADOS\resultado_con_geocerca.csv"
shp_path = r"C:\Sahpe SAnta Ana\Extpan_Santa_Ana24-25.shp"
output_folder = r"C:\Users\AAUTOAGRI01\OneDrive - Compania Agricola Industrial Santa Ana, S. A\Documentos\FERTILIZACION\RESULTADOS\comparativos PDF PANTE1"

shp_grilla_path = r"C:\Users\AAUTOAGRI01\OneDrive - Compania Agricola Industrial Santa Ana, S. A\Documentos\FERTILIZACION\shapefiles_combinados.shp"

# Cargar imágenes de logos y norte
logo_izq = mpimg.imread(r"C:\Users\AAUTOAGRI01\OneDrive - Compania Agricola Industrial Santa Ana, S. A\Imágenes\Santa Ana\Logos oficiales\Logos oficiales\logo-fondo-verde.png")
logo_der = mpimg.imread(r"C:\Users\AAUTOAGRI01\OneDrive - Compania Agricola Industrial Santa Ana, S. A\Imágenes\Santa Ana\Logo_AP.jpg")
img_norte = mpimg.imread(r"C:\Users\AAUTOAGRI01\OneDrive - Compania Agricola Industrial Santa Ana, S. A\Imágenes\Santa Ana\BRUJULA2.png")

# ==================== CONFIGURACIÓN ====================
A4_HORIZONTAL = (16.5, 11.7)  # Tamaño del PDF horizontal en pulgadas (A4)
POS_LOGO = [0.25, 0.85, 0.5, 0.1]  # Posición del logo como [x, y, ancho, alto] en % del PDF
CAJETIN_WIDTH = 15
CAJETIN_HEIGHT = 9
CAJETIN_X = (16.5 - CAJETIN_WIDTH) / 2
CAJETIN_Y = (11.7 - CAJETIN_HEIGHT) / 2 - 0.5
CAJETIN_POS = [
    CAJETIN_X / 16.5,
    CAJETIN_Y / 11.7,
    CAJETIN_WIDTH / 16.5,
    CAJETIN_HEIGHT / 11.7
]

# ==================== CARGA DE DATOS ====================
geocercas = gpd.read_file(shp_path)
diseno1 = gpd.read_file(shp_diseno_path)
diseno2 = gpd.read_file(shp_diseno_path2)
diseno = pd.concat([diseno1, diseno2], ignore_index=True)
diseno['DOSIS'] = pd.to_numeric(diseno['DOSIS'], errors='coerce')

# Leer la grilla combinada
grilla_original = gpd.read_file(shp_grilla_path)

# Asignar un CRS si no lo tiene
if grilla_original.crs is None:
    grilla_original.set_crs(epsg=4326, inplace=True)

# Asegurar que la grilla tenga el mismo CRS que las geocercas
if grilla_original.crs != geocercas.crs:
    grilla_original = grilla_original.to_crs(geocercas.crs)

# Asegurar sistema de coordenadas
if grilla_original.crs is None:
    grilla_original.set_crs("EPSG:4326", inplace=True)
if grilla_original.crs != geocercas.crs:
    grilla_original = grilla_original.to_crs(geocercas.crs)

# Recortar grilla con geocercas base y asignar atributos
grilla_recortada = gpd.overlay(grilla_original, geocercas, how='intersection')

# Asegurar columna 'intensidad' con base en 'AppldRate'
grilla_recortada['intensidad'] = pd.to_numeric(grilla_recortada['AppldRate'], errors='coerce')
grilla_recortada = grilla_recortada[grilla_recortada['intensidad'] > 0].copy()

# Convertir columna de dosis
grilla_recortada['dosis'] = pd.to_numeric(grilla_recortada['AppldRate'], errors='coerce')
grilla_recortada = grilla_recortada[grilla_recortada['dosis'] > 0].copy()


# Paleta de colores base para clasificación de etiquetas
colores = [
    "#1b9e77", "#d95f02", "#7570b3", "#e7298a", "#66a61e",
    "#e6ab02", "#a6761d", "#666666", "#66c2a5", "#fc8d62",
    "#8da0cb", "#e78ac3", "#a6d854", "#ffd92f", "#e5c494",
    "#b3b3b3", "#a6cee3", "#1f78b4", "#b2df8a", "#33a02c",
    "#fb9a99", "#e31a1c", "#fdbf6f", "#ff7f00", "#cab2d6",
    "#6a3d9a", "#ffff99", "#b15928", "#fbb4ae", "#b3cde3",
    "#ccebc5", "#decbe4", "#fed9a6", "#ffffcc", "#e5d8bd",
    "#fddaec", "#f2f2f2", "#e41a1c", "#377eb8", "#4daf4a",
    "#984ea3", "#ff7f00", "#ffff33", "#a65628", "#f781bf",
    "#999999", "#66c2a5", "#fc8d62", "#8da0cb", "#e78ac3"
]


def calcular_orientacion(polygon_gdf):
    utm = polygon_gdf.to_crs("EPSG:32616")
    rect = utm.geometry.unary_union.minimum_rotated_rectangle
    coords = list(rect.exterior.coords)
    dists = [np.hypot(coords[i+1][0] - coords[i][0], coords[i+1][1] - coords[i][1]) for i in range(len(coords)-1)]
    idx = np.argmax(dists)
    (x1, y1), (x2, y2) = coords[idx], coords[idx+1]
    return np.degrees(np.arctan2(y2 - y1, x2 - x1))


def generar_grilla_orientada(poligono_base, cell_width=5, cell_height=20):
    from shapely import affinity
    import pyproj

    # Convertir a UTM para trabajar en metros
    utm_crs = "EPSG:32616"  # Ajusta a tu zona si es diferente
    poligono_utm = poligono_base.to_crs(utm_crs)

    # Calcular orientación
    angle = calcular_orientacion(poligono_utm)

    # Rotar para alinear el bounding box
    rotado = poligono_utm.copy()
    rotado['geometry'] = rotado['geometry'].rotate(-angle, origin='centroid')

    # Generar grilla rectangular en coordenadas rotadas
    xmin, ymin, xmax, ymax = rotado.total_bounds
    x_coords = np.arange(xmin, xmax, cell_width)
    y_coords = np.arange(ymin, ymax, cell_height)

    celdas = [
        Polygon([
            (x, y),
            (x + cell_width, y),
            (x + cell_width, y + cell_height),
            (x, y + cell_height)
        ])
        for x in x_coords for y in y_coords
    ]

    grilla_rotada = gpd.GeoDataFrame(geometry=celdas, crs=utm_crs)

    # Rotar de regreso
    grilla = grilla_rotada.copy()
    grilla['geometry'] = grilla['geometry'].rotate(angle, origin=poligono_utm.geometry.unary_union.centroid)

    # Intersecar con el polígono original
    grilla = grilla.to_crs(poligono_base.crs)  # Volver al CRS original
    poligono_base = poligono_base.to_crs(grilla.crs)
    return gpd.overlay(grilla, poligono_base, how='intersection')


def asignar_dosis_a_grilla(grilla, datos):
    if datos.empty or grilla.empty:
        return grilla
    tree = cKDTree(np.array(list(zip(datos.geometry.x, datos.geometry.y))))
    centroids = np.array([(geom.centroid.x, geom.centroid.y) for geom in grilla.geometry])
    _, idxs = tree.query(centroids, k=1)
    grilla['dosis'] = datos.iloc[idxs]['intensidad'].values
    return grilla


def clasificar_dosis_segun_diseno(grilla, valores_diseño):
    etiquetas = []
    valores = sorted([v for v in valores_diseño if not pd.isna(v)])

    if not valores:
        grilla['etiqueta'] = "Sin diseño"
        return grilla

    min_d = valores[0]
    max_d = valores[-1]

    for dosis in grilla['dosis']:
        if dosis < min_d * 0.95:
            etiquetas.append("Menor al diseño")
        elif dosis > max_d * 1.05:
            etiquetas.append("Mayor al diseño")
        else:
            for i in range(len(valores) - 1):
                lim_inf = valores[i]
                lim_sup = valores[i + 1] * 0.95
                if lim_inf <= dosis < lim_sup:
                    etiquetas.append(f"{int(valores[i])}")
                    break
            else:
                etiquetas.append(f"{int(valores[-1])}")

    grilla['etiqueta'] = etiquetas
    return grilla



def etiquetar_dosis(grilla, valores_diseño):
    return clasificar_dosis_segun_diseno(grilla, valores_diseño).rename(columns={'etiqueta': 'etiqueta_dosis'})


def calcular_cumplimiento_celda_a_celda(grilla_clasificada, diseno_filtrado):
    try:
        inter = gpd.overlay(grilla_clasificada, diseno_filtrado[['etiqueta', 'geometry']],
                            how='intersection', keep_geom_type=False)
        columnas_etiqueta = [col for col in inter.columns if col.startswith('etiqueta')]
        inter['dosis_ejecutada'] = inter[columnas_etiqueta[0]].str.extract(r'(\d+)').astype(float)
        inter['dosis_disenada'] = inter[columnas_etiqueta[1]].str.extract(r'(\d+)').astype(float)
        inter['% cumplimiento'] = (inter['dosis_ejecutada'] / inter['dosis_disenada']) * 100
        inter['clasificacion'] = inter['% cumplimiento'].apply(
            lambda p: '<90%' if p < 90 else ('>110%' if p > 110 else '90–110%')
        )
        return inter
    except Exception as e:
        print(f"⚠ Error al calcular cumplimiento: {e}")
        return None

def generar_csv_masivo(output_csv_path):
    resumen_global = []

    for pante1 in grilla['PANTE1'].dropna().unique():
        datos_pante = grilla[grilla['PANTE1'] == pante1]
        if datos_pante.empty:
            continue

        region = datos_pante['REGION'].iloc[0] if 'REGION' in datos_pante.columns else "NA"
        finca = datos_pante['FINCA'].iloc[0] if 'FINCA' in datos_pante.columns else "NA"

        diseno_filtrado = diseno[diseno['PANTE1'] == pante1].copy()
        if diseno_filtrado.empty:
            continue

        # === Calcular por bloque ===
        inter_bloques = gpd.overlay(
            datos_pante[['geometry', 'intensidad']], 
            diseno_filtrado[['geometry', 'DOSIS']], 
            how='intersection'
        )
        inter_bloques = inter_bloques.to_crs("EPSG:32616")
        inter_bloques['area_ha'] = inter_bloques.geometry.area / 10000
        inter_bloques['dosis_area'] = inter_bloques['intensidad'] * inter_bloques['area_ha']

        bloques_prom = inter_bloques.groupby('DOSIS').agg({
            'dosis_area': 'sum',
            'area_ha': 'sum',
        }).reset_index()
        bloques_prom['ejecutado_prom'] = bloques_prom['dosis_area'] / bloques_prom['area_ha']
        bloques_prom['diferencia'] = bloques_prom['ejecutado_prom'] - bloques_prom['DOSIS']
        bloques_prom['diferencia_pct'] = 100 * bloques_prom['diferencia'] / bloques_prom['DOSIS']

        # Preparar diseno_dif con ID_BLOQUE
        diseno_dif = diseno_filtrado.copy()
        diseno_dif = diseno_dif.to_crs("EPSG:32616")
        diseno_dif['area_real_ha'] = diseno_dif.geometry.area / 10000
        diseno_dif = diseno_dif.reset_index(drop=True)
        diseno_dif['ID_BLOQUE'] = ['B{}'.format(i+1) for i in range(len(diseno_dif))]

        diseno_dif = diseno_dif.merge(
            bloques_prom[['DOSIS', 'ejecutado_prom', 'diferencia', 'diferencia_pct']],
            left_on='DOSIS', right_on='DOSIS', how='left'
        )

        resumen_bloques = diseno_dif[['ID_BLOQUE', 'DOSIS', 'ejecutado_prom', 'diferencia', 'diferencia_pct', 'area_real_ha', 'geometry']].dropna()
        resumen_bloques['categoria_cumplimiento'] = resumen_bloques['diferencia_pct'].apply(
            lambda pct: "SUBDOSIFICADO" if pct < -5 else ("SOBREDOSIFICADO" if pct > 5 else "EN RANGO")
        )

        # === Resumen por categoría ===
        area_total = resumen_bloques['area_real_ha'].sum()
        resumen_categoria = []
        for cat in resumen_bloques['categoria_cumplimiento'].unique():
            grupo = resumen_bloques[resumen_bloques['categoria_cumplimiento'] == cat]
            area_total_cat = grupo['area_real_ha'].sum()
            pct_area_cat = (area_total_cat / area_total) * 100
            var_pond_cat = np.average(grupo['diferencia_pct'], weights=grupo['area_real_ha'])
            resumen_categoria.append((cat, pct_area_cat, area_total_cat, var_pond_cat))

        # === Armar el dataframe global ===
        for idx, row in resumen_bloques.iterrows():
            resumen_global.append({
                'REGIO': region,  # ya con REGIO correcto
                'FINCA': finca,
                'PANTE1': pante1,
                'ID_BLOQUE': row['ID_BLOQUE'],
                'DOSIS_EJECUTADA': round(row['ejecutado_prom'], 2),
                'DOSIS_DISEÑO': row['DOSIS'],
                'DIFERENCIA_KG_HA': round(row['diferencia'], 2),
                'DIFERENCIA_PCT': round(row['diferencia_pct'], 2),
                'AREA_HA': round(row['area_real_ha'], 2),
                'CATEGORIA_CUMPLIMIENTO': row['categoria_cumplimiento'],
                '%_DEL_TOTAL_CAT_CUMPLIMIENTO': next(
                    (round(x[1], 1) for x in resumen_categoria if x[0] == row['categoria_cumplimiento']), None
                ),
                'AREA_CAT_CUMPLIMIENTO': next(
                    (round(x[2], 2) for x in resumen_categoria if x[0] == row['categoria_cumplimiento']), None
                ),
                '%_VARIACIÓN_CATEGORIA_CUMPLIMIENTO': next(
                    (round(x[3], 2) for x in resumen_categoria if x[0] == row['categoria_cumplimiento']), None
                ),
                'geometry': row['geometry']  # Agrega la geometría del bloque
            })

    # === Exportar a CSV ===
    df_final = gpd.GeoDataFrame(resumen_global, geometry='geometry', crs="EPSG:32616")

    # Exportar con todas las 14 columnas (incluyendo geometry WKT)
    df_final['geometry'] = df_final['geometry'].apply(lambda g: g.wkt if g is not None else None)
    df_final.to_csv(output_csv_path, index=False, encoding='utf-8-sig')
    print(f"✅ CSV masivo generado: {output_csv_path}")


# ==================== GENERAR CSV MASIVO DE RESUMEN DE BLOQUES + RESUMEN CATEGORIAS ====================

import csv
from datetime import datetime

# Crear timestamp YYYYMMDD_HHMM
timestamp_str = datetime.now().strftime("%Y%m%d_%H%M")

# Ruta de salida para el CSV
csv_output_path = r"C:\Users\AAUTOAGRI01\OneDrive - Compania Agricola Industrial Santa Ana, S. A\Documentos\FERTILIZACION\RESULTADOS\Resumen_Masivo_PANTE1_{}.csv"

# Lista para ir acumulando los registros de todos los PANTE1
resumen_global = []

# Obtener lista de PANTE1 únicos
pantes_unicos = grilla_original['PANTE1'].dropna().unique()

# Iterar sobre TODOS los PANTE1
for pante1 in pantes_unicos:
    try:
        datos_pante = grilla[grilla['PANTE1'] == pante1]
        if datos_pante.empty:
            print(f"⚠ Sin datos para PANTE1 {pante1}")
            continue

        region = datos_pante['REGION'].iloc[0] if 'REGION' in datos_pante.columns else "NA"
        finca = datos_pante['FINCA'].iloc[0] if 'FINCA' in datos_pante.columns else "NA"

        diseno_filtrado = diseno[diseno['PANTE1'] == pante1].copy()
        if diseno_filtrado.empty:
            print(f"⚠ Sin diseño para PANTE1 {pante1}")
            continue

        diseno_filtrado['etiqueta'] = diseno_filtrado['DOSIS'].astype(int).astype(str)
        valores_diseño = diseno_filtrado['DOSIS'].dropna().unique()
        if len(valores_diseño) == 0:
            print(f"⚠ Sin valores válidos de dosis para PANTE1 {pante1}")
            continue

        # Usa el polígono original del bloque como borde (no una grilla)
        grilla = diseno_filtrado[['geometry']].copy()
        grilla = asignar_dosis_a_grilla(grilla, datos_pante)
        if grilla.empty:
            print(f"⚠ Grilla vacía para PANTE1 {pante1}")
            continue

        grilla = etiquetar_dosis(grilla, valores_diseño)
        grilla_clasificada = clasificar_dosis_segun_diseno(grilla.copy(), valores_diseño)
        cumplimiento = calcular_cumplimiento_celda_a_celda(grilla_clasificada.copy(), diseno_filtrado)
        if cumplimiento is None or cumplimiento.empty:
            print(f"⚠ No se pudo calcular cumplimiento para PANTE1 {pante1}")
            continue

        # === Calculo resumen por bloque ===
        inter_bloques = gpd.overlay(grilla[['geometry', 'dosis']], diseno_filtrado[['geometry', 'etiqueta', 'DOSIS']], how='intersection')
        inter_bloques = inter_bloques.to_crs("EPSG:32616")
        inter_bloques['area_ha'] = inter_bloques.geometry.area / 10000
        inter_bloques['dosis_area'] = inter_bloques['dosis'] * inter_bloques['area_ha']

        bloques_prom = inter_bloques.groupby('etiqueta').agg({
            'dosis_area': 'sum',
            'area_ha': 'sum',
            'DOSIS': 'first',
            'geometry': 'first'  # Agrega la geometría del bloque
        }).reset_index()

        bloques_prom['ejecutado_prom'] = bloques_prom['dosis_area'] / bloques_prom['area_ha']
        bloques_prom['diferencia'] = bloques_prom['ejecutado_prom'] - bloques_prom['DOSIS']
        bloques_prom['diferencia_pct'] = 100 * bloques_prom['diferencia'] / bloques_prom['DOSIS']

        # Asignar ID_BLOQUE
        bloques_prom = bloques_prom.reset_index(drop=True)
        bloques_prom['ID_BLOQUE'] = ['B{}'.format(i+1) for i in range(len(bloques_prom))]

        # Clasificación en categorías
        def clasificar_categoria(pct):
            if pct < -5:
                return "SUBDOSIFICADO"
            elif pct > 5:
                return "SOBREDOSIFICADO"
            else:
                return "EN RANGO"

        bloques_prom['categoria_cumplimiento'] = bloques_prom['diferencia_pct'].apply(clasificar_categoria)

        # === Resumen por categoría (para completar las columnas que pediste) ===
        area_total = bloques_prom['area_ha'].sum()

        resumen_categoria = bloques_prom.groupby('categoria_cumplimiento').agg({
            'area_ha': 'sum',
            'diferencia_pct': lambda x: np.average(x, weights=bloques_prom.loc[x.index, 'area_ha'])
        }).reset_index()

        resumen_categoria['%_DEL_TOTAL_CAT_CUMPLIMIENTO'] = 100 * resumen_categoria['area_ha'] / area_total

        # --- Ahora "expandimos" esos valores para que cada bloque también los tenga --- 
        bloques_prom = bloques_prom.merge(resumen_categoria, on='categoria_cumplimiento', how='left', suffixes=('', '_CAT'))

        # Agregar columnas PANTE1, REGION, FINCA
        bloques_prom['PANTE1'] = pante1
        bloques_prom['REGION'] = region
        bloques_prom['FINCA'] = finca

        # --- Finalmente agregar al resumen_global las columnas en el orden que tú pediste ---
        resumen_global.append(bloques_prom[[
            'REGION', 'FINCA', 'PANTE1', 'ID_BLOQUE',
            'ejecutado_prom', 'DOSIS', 'diferencia', 'diferencia_pct', 'area_ha',
            'categoria_cumplimiento', '%_DEL_TOTAL_CAT_CUMPLIMIENTO', 'area_ha_CAT', 'diferencia_pct_CAT', 'geometry'
        ]])

        print(f"✅ Resumen generado para PANTE1 {pante1}")

    except Exception as e:
        print(f"⚠ Error al procesar PANTE1 {pante1}: {e}")

# ==================== UNIR Y GUARDAR CSV ====================

if resumen_global:
    df_resumen_global = pd.concat(resumen_global, ignore_index=True)

    df_resumen_global.rename(columns={
        'REGION': 'REGION',
        'FINCA': 'FINCA',
        'PANTE1': 'PANTE1',
        'ID_BLOQUE': 'ID_BLOQUE',
        'ejecutado_prom': 'DOSIS_EJECUTADA',
        'DOSIS': 'DOSIS_DISEÑO',
        'diferencia': 'DIFERENCIA_KG_HA',
        'diferencia_pct': 'DIFERENCIA_PCT',
        'area_ha': 'AREA_HA',
        'categoria_cumplimiento': 'CATEGORIA_CUMPLIMIENTO',
        '%_DEL_TOTAL_CAT_CUMPLIMIENTO': '%_DEL_TOTAL_CAT_CUMPLIMIENTO',
        'area_ha_CAT': 'AREA_CAT_CUMPLIMIENTO',
        'diferencia_pct_CAT': '%_VARIACIÓN_CATEGORIA_CUMPLIMIENTO',
        'geometry': 'geometry'
    }, inplace=True)

    # Exportar geometry como WKT
    df_resumen_global['geometry'] = df_resumen_global['geometry'].apply(lambda g: g.wkt if g is not None else None)

    # Guardar CSV (sobrescribe el archivo si ya existe)
    df_resumen_global.to_csv(csv_output_path, index=False, encoding='utf-8-sig', mode='w')

    # Exportar también como JSON para Power BI (geometry en WKT)
    json_output_path = csv_output_path.replace('.csv', '.json')
    df_resumen_global.to_json(json_output_path, orient='records', force_ascii=False)

    # Exportar como GeoJSON (geometry en formato geojson)
    geojson_output_path = csv_output_path.replace('.csv', '.geojson')
    gdf_geojson = gpd.GeoDataFrame(df_resumen_global, geometry=gpd.GeoSeries.from_wkt(df_resumen_global['geometry']), crs="EPSG:32616")
    gdf_geojson.to_file(geojson_output_path, driver='GeoJSON')

    print(f"\n✅ CSV masivo generado: {csv_output_path}")
    print(f"✅ JSON masivo generado: {json_output_path}")
    print(f"✅ GeoJSON masivo generado: {geojson_output_path}")
else:
    print("\n⚠ No se generó ningún resumen, no hay datos válidos.")
