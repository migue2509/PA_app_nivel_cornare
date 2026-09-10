"""Panel académico de nivel: ejecutar con streamlit run app_nivel_cornare.py."""

from datetime import datetime
import math
from urllib.parse import urljoin, urlparse

import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st

ESTUDIANTE = "Miguel Angel Ospina Rua"
CODIGO_ESTACION = "8"
NOMBRE_ESTACION = "Argelia"
FECHA_DESDE = "2026-08-23"
FECHA_HASTA = "2026-08-30"
CALIDAD = 1
API_BASE = "https://marco.cornare.gov.co/api/v1/estaciones"
GEOPORTAL = "https://marco.cornare.gov.co/geoportal"
VERDE, AGUA, AMBAR = "#16735B", "#229C9C", "#C68B32"


def pedir_json(session, url, params=None):
    """Conserva TLS verificado; no sigue redirecciones a otros servidores."""
    respuesta = session.get(
    url,
    params=params,
    timeout=(8, 25),
    allow_redirects=False,
    verify=False,
    )
    respuesta.raise_for_status()
    if respuesta.status_code != 200:
        raise ValueError(f"Respuesta inesperada: HTTP {respuesta.status_code}.")
    try:
        datos = respuesta.json()
    except ValueError as exc:
        raise ValueError("El servicio no devolvió JSON válido.") from exc
    if not isinstance(datos, dict):
        raise ValueError("La respuesta no tiene el formato de objeto esperado.")
    return datos


@st.cache_data(ttl=900, show_spinner=False)
def consultar_niveles(codigo, desde, hasta, calidad):
    """Carga todas las páginas. Una descarga parcial nunca se muestra como completa."""
    url = f"{API_BASE}/{codigo}/nivel"
    registros, visitadas = [], set()
    with requests.Session() as session:
        session.headers.update({"Accept": "application/json", "User-Agent": "ArgeliaDashboard/1.0"})
        pagina = pedir_json(session, url, {"desde": desde, "hasta": hasta, "calidad": calidad})
        primera = pagina
        for _ in range(100):
            if "values" not in pagina or not isinstance(pagina["values"], list):
                raise ValueError("La API no contiene una lista 'values'. Revisa el esquema del servicio.")
            if not all(isinstance(item, dict) for item in pagina["values"]):
                raise ValueError("La API contiene registros con un formato inesperado.")
            registros.extend(pagina["values"])
            siguiente = pagina.get("next")
            if not siguiente:
                return registros, primera, datetime.now().astimezone().isoformat(timespec="seconds")
            if not isinstance(siguiente, str):
                raise ValueError("La dirección de paginación no es válida.")
            url = urljoin(url, siguiente)
            parsed = urlparse(url)
            if parsed.scheme != "https" or parsed.netloc != urlparse(API_BASE).netloc:
                raise ValueError("La siguiente página apunta a un origen diferente al de CORNARE.")
            if url in visitadas:
                raise ValueError("La API repite una página; se detuvo la consulta para evitar duplicados.")
            visitadas.add(url)
            pagina = pedir_json(session, url)
    raise ValueError("Se alcanzó el límite de 100 páginas. No se muestran resultados incompletos.")


def limpiar_registros(registros):
    df = pd.DataFrame(registros)
    if df.empty:
        return pd.DataFrame(columns=["fecha", "nivel"]), 0, 0
    if not {"level_date", "level"}.issubset(df.columns):
        raise ValueError("Faltan las columnas 'level_date' o 'level' en los registros.")
    df = df.rename(columns={"level_date": "fecha", "level": "nivel"})[["fecha", "nivel"]]
    # Sin zona explícita se conserva la hora informada; con zona se convierte a Colombia.
    def fecha_local(valor):
        if not isinstance(valor, str):
            return pd.NaT
        try:
            fecha = pd.Timestamp(valor)
            if fecha.tzinfo is not None:
                fecha = fecha.tz_convert("America/Bogota").tz_localize(None)
            return fecha
        except (ValueError, TypeError, OverflowError):
            return pd.NaT
    df["fecha"] = df["fecha"].map(fecha_local)
    df["nivel"] = pd.to_numeric(df["nivel"], errors="coerce")
    df["nivel"] = df["nivel"].replace([float("inf"), -float("inf")], float("nan"))
    validos = df.dropna(subset=["fecha", "nivel"]).copy()
    descartados = len(df) - len(validos)
    duplicados = int(validos.duplicated("fecha", keep="last").sum())
    validos = validos.drop_duplicates("fecha", keep="last").sort_values("fecha").reset_index(drop=True)
    return validos, descartados, duplicados


def analizar_calidad(df):
    """Cobertura aproximada entre primera y última lectura, sin crear rejillas enormes."""
    q1, q3 = df["nivel"].quantile([0.25, 0.75])
    iqr = q3 - q1
    # Un nivel negativo puede ser válido según el datum; no se marca automáticamente.
    atipicos = (df["nivel"] < q1 - 1.5 * iqr) | (df["nivel"] > q3 + 1.5 * iqr)
    resultado = {"atipicos": atipicos, "huecos": None, "indice": None, "frecuencia": None}
    diferencias = df["fecha"].diff().dropna()
    positivas = diferencias[diferencias > pd.Timedelta(0)]
    if len(positivas) < 2:
        return resultado
    moda = positivas.mode()
    # No inferir una periodicidad cuando todas las diferencias son distintas.
    if len(moda) != 1:
        return resultado
    frecuencia = moda.iloc[0]
    cocientes = positivas / frecuencia
    # Tolerancia del 5% a variaciones de timestamp; serie irregular => no estimable.
    if not all(abs(float(x) - round(float(x))) <= 0.05 for x in cocientes):
        return resultado
    huecos = sum(max(0, round(float(x)) - 1) for x in cocientes)
    cobertura = len(df) / (len(df) + huecos)
    resultado.update(huecos=huecos, frecuencia=frecuencia,
                     indice=round((0.7 * cobertura + 0.3 * (1 - atipicos.mean())) * 100, 1))
    return resultado


def detectar_coordenadas(datos):
    """Solo campos de coordenadas explícitos; nunca sustituye otra ubicación."""
    candidatos = [datos]
    for llave in ("station", "estacion", "metadata"):
        if isinstance(datos.get(llave), dict):
            candidatos.append(datos[llave])
    for item in candidatos:
        lat = next((item[k] for k in ("lat", "latitude", "latitud") if k in item), None)
        lon = next((item[k] for k in ("lng", "lon", "longitude", "longitud") if k in item), None)
        try:
            lat, lon = float(lat), float(lon)
            if math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180:
                return lat, lon
        except (ValueError, TypeError):
            continue
    return None


def mostrar_grafico(figura):
    figura.update_layout(template="plotly_white", height=340,
                          margin=dict(l=20, r=20, t=30, b=25),
                          font=dict(family="Arial", color="#24483D"),
                          paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                          legend=dict(orientation="h", y=-0.22),
                          hoverlabel=dict(bgcolor="white"))
    figura.update_xaxes(showgrid=False)
    figura.update_yaxes(gridcolor="#E1ECE5", zeroline=False)
    st.plotly_chart(figura, width="stretch", theme=None,
                    config={"displaylogo": False, "scrollZoom": False})


def main():
    st.set_page_config(page_title="Argelia · Observatorio del agua", page_icon="🌿", layout="wide")
    st.markdown("""<style>
    .stApp {background:#F3F7F1; color:#23473B;}
    .block-container {max-width:1280px; padding-top:2rem; padding-bottom:3rem;}
    .hero {position:relative; overflow:hidden; background:linear-gradient(115deg,#113E33,#18664F 65%,#2D8980);
      color:white; padding:42px; border-radius:26px; margin-bottom:24px;}
    .hero::after {content:'≈';position:absolute;right:25px;top:-70px;font-size:320px;opacity:.08;}
    .hero h1 {color:white; font-size:clamp(2.3rem,5vw,4rem);line-height:1.12;margin:12px 0;}
    .hero p {max-width:660px; color:#DEEEE5; font-size:1.05rem;}
    .eyebrow {letter-spacing:3px;font-size:12px;font-weight:700;}
    .pill {display:inline-block;padding:7px 13px;border:1px solid #70A58E;border-radius:30px;
      font-size:12px;margin:5px 6px 0 0;color:#EEFAF2;}
    [data-testid="stMetric"] {background:white;border:1px solid #DEEBDE;border-radius:18px;padding:20px;}
    [data-testid="stMetricValue"] {color:#16604A;}
    [data-testid="stVerticalBlockBorderWrapper"] {border-radius:18px;}
    .footer {margin-top:24px;padding-top:18px;border-top:1px solid #D7E6D7;font-size:13px;color:#547166;}
    @media(max-width:640px){.hero{padding:25px}.block-container{padding-top:1rem}}
    </style>""", unsafe_allow_html=True)
    st.markdown(f"""<div class="hero"><div class="eyebrow">OBSERVATORIO DEL AGUA · CORNARE / MARCO</div>
    <h1>Argelia, al ritmo del agua.</h1>
    <p>Una mirada al comportamiento del nivel y a la continuidad de sus registros.</p>
    <span class="pill">ESTACIÓN 08 · ARGELIA</span><span class="pill">{ESTUDIANTE}</span>
    <span class="pill">23–30 AGOSTO 2026</span></div>""", unsafe_allow_html=True)
    st.caption("Consulta automática · Código y nombre configurados según la asignación académica · Filtro de calidad: 1")
    with st.spinner("Conectando con MARCO y leyendo las mediciones…"):
        try:
            registros, metadata, consultado = consultar_niveles(CODIGO_ESTACION, FECHA_DESDE, FECHA_HASTA, CALIDAD)
            df, descartados, duplicados = limpiar_registros(registros)
        except requests.exceptions.SSLError:
            st.error("No se pudo verificar el certificado HTTPS de MARCO. La conexión segura no se completó.")
            st.info("Comprueba el certificado del servicio y los certificados de tu equipo antes de volver a cargar la página.")
            st.link_button("Abrir el geoportal de CORNARE ↗", GEOPORTAL)
            return
        except (requests.exceptions.RequestException, ValueError) as exc:
            st.error("No fue posible completar la consulta de nivel de la estación 8.")
            st.info("El servicio puede estar temporalmente fuera de línea o usar otro esquema. Vuelve a cargar la página para reintentar.")
            with st.expander("Detalle de la consulta"):
                st.code(str(exc))
            st.link_button("Abrir el geoportal de CORNARE ↗", GEOPORTAL)
            return
    if df.empty:
        st.warning("No hay mediciones válidas para la estación 8 entre el 23 y el 30 de agosto de 2026 con calidad 1.")
        st.caption(f"Registros recibidos: {len(registros)} · Descartados por fecha o valor inválido: {descartados}.")
        st.link_button("Consultar disponibilidad en MARCO ↗", GEOPORTAL)
        return

    analisis = analizar_calidad(df)
    df["atipico_iqr"] = analisis["atipicos"]
    cantidad_atipicos = int(df["atipico_iqr"].sum())
    diario = df.set_index("fecha")["nivel"].resample("D").agg(["mean", "min", "max", "count"])
    st.caption(f"Lecturas disponibles: {df.fecha.min():%d/%m/%Y %H:%M} → {df.fecha.max():%d/%m/%Y %H:%M} · Consulta: {consultado}")
    st.caption("Nivel en la unidad original de la API; unidad y datum no verificados. Fechas sin zona: hora informada por la fuente.")
    columnas = st.columns(4)
    columnas[0].metric("Último nivel", f"{df.nivel.iloc[-1]:.2f}")
    columnas[1].metric("Nivel promedio", f"{df.nivel.mean():.2f}")
    columnas[2].metric("Lecturas disponibles", f"{len(df):,}")
    columnas[3].metric("Calidad estimada", "No estimable" if analisis["indice"] is None else f"{analisis['indice']:.1f} / 100")

    st.subheader("01 / El pulso de la estación")
    with st.container(border=True):
        figura = go.Figure(go.Scatter(x=df.fecha, y=df.nivel, mode="lines+markers" if len(df) == 1 else "lines",
                                     name="Nivel", line=dict(color=VERDE, width=2.5)))
        if cantidad_atipicos:
            atipicos = df[df.atipico_iqr]
            figura.add_trace(go.Scatter(x=atipicos.fecha, y=atipicos.nivel, mode="markers", name="Atípico IQR",
                                       marker=dict(color=AMBAR, size=8)))
        figura.update_layout(yaxis_title="Nivel · unidad de la API", xaxis_title="Fecha", hovermode="x unified")
        mostrar_grafico(figura)
        st.caption("La línea une las observaciones disponibles; no implica mediciones continuas entre puntos.")

    st.subheader("02 / Los datos, desde otra perspectiva")
    izquierda, derecha = st.columns([1.4, 1])
    with izquierda, st.container(border=True):
        st.markdown("**Nivel promedio por día**")
        figura = go.Figure(go.Bar(x=diario.index, y=diario["mean"], marker_color=AGUA, name="Promedio",
                                 hovertemplate="%{x|%d/%m/%Y}<br>Nivel promedio: %{y:.2f}<extra></extra>"))
        figura.update_layout(yaxis_title="Nivel · unidad de la API", xaxis_title="Día")
        mostrar_grafico(figura)
        st.caption("Promedio de las lecturas recibidas en cada día. Los días sin datos quedan vacíos.")
    with derecha, st.container(border=True):
        st.markdown("**Distribución de lecturas · torta**")
        figura = go.Figure(go.Pie(labels=["Dentro del rango IQR", "Atípicas IQR"],
                                 values=[len(df) - cantidad_atipicos, cantidad_atipicos], hole=.68,
                                 sort=False, marker=dict(colors=[VERDE, AMBAR]), textinfo="percent"))
        figura.add_annotation(text=f"<b>{len(df):,}</b><br>lecturas", x=.5, y=.5, showarrow=False, font_size=21)
        mostrar_grafico(figura)
        st.caption("Una lectura atípica es una señal estadística para revisar; puede reflejar un evento real.")

    st.subheader("03 / Hallazgos del período")
    pico = df.loc[df.nivel.idxmax()]
    cambio = df.nivel.iloc[-1] - df.nivel.iloc[0]
    a, b, c = st.columns(3)
    with a, st.container(border=True):
        st.markdown("🌊 **Mayor nivel observado**")
        st.markdown(f"### {pico.nivel:.2f}")
        st.caption(f"Registrado el {pico.fecha:%d/%m/%Y a las %H:%M}.")
    with b, st.container(border=True):
        st.markdown("🌱 **Cambio entre extremos**")
        st.markdown(f"### {cambio:+.2f}" if len(df) > 1 else "### No estimable")
        st.caption("Diferencia entre la última y la primera lectura; no es un pronóstico ni una tendencia estadística.")
    with c, st.container(border=True):
        st.markdown("🔎 **Lecturas para revisar**")
        st.markdown(f"### {cantidad_atipicos / len(df):.1%}")
        st.caption(f"{cantidad_atipicos} de {len(df)} lecturas fuera del rango IQR.")
    with st.container(border=True):
        st.markdown("**Continuidad de los reportes por día**")
        figura = go.Figure(go.Bar(x=diario.index, y=diario["count"], marker_color=VERDE, name="Lecturas"))
        figura.update_layout(yaxis_title="Número de lecturas", xaxis_title="Día")
        mostrar_grafico(figura)

    st.subheader("04 / Territorio y transparencia")
    izquierda, derecha = st.columns(2)
    with izquierda, st.container(border=True):
        st.markdown("**Argelia · estación 8**")
        coords = detectar_coordenadas(metadata)
        if coords:
            st.map(pd.DataFrame({"lat": [coords[0]], "lon": [coords[1]]}), zoom=11)
            st.caption("Coordenadas incluidas en la respuesta de esta consulta.")
        else:
            st.info("La respuesta no contiene coordenadas reconocibles de la estación. Consulta su ubicación en el geoportal.")
        st.link_button("Explorar el geoportal MARCO ↗", GEOPORTAL)
    with derecha, st.container(border=True):
        st.markdown("**Cómo se interpretan los datos**")
        st.write("Índice académico = 70% de cobertura temporal estimada + 30% de lecturas no atípicas. No representa calidad del agua ni riesgo de inundación.")
        st.write("Atípicos: valores fuera de [Q1 − 1.5 × IQR, Q3 + 1.5 × IQR]. Se conservan en los gráficos y promedios. Un nivel negativo no se considera inválido por sí solo.")
        if analisis["huecos"] is None:
            st.caption("Cobertura no estimable: faltan lecturas o no se puede inferir una frecuencia regular.")
        else:
            st.caption(f"Intervalo inferido: {analisis['frecuencia']} · Huecos estimados: {analisis['huecos']}. Solo entre la primera y última lectura; no incluye los bordes del rango solicitado.")
        st.caption(f"Registros inválidos descartados: {descartados} · Fechas duplicadas eliminadas: {duplicados} (se conserva la última).")
    with st.expander("Ver mediciones procesadas y descargar"):
        st.dataframe(df, width="stretch", hide_index=True)
        st.download_button("Descargar mediciones CSV", df.to_csv(index=False).encode("utf-8-sig"),
                           file_name="nivel_argelia_estacion_8.csv", mime="text/csv")
    st.markdown(f'<div class="footer">🌿 {ESTUDIANTE} · Proyecto académico · Fuente: CORNARE / MARCO</div>', unsafe_allow_html=True)


if __name__ == "__main__":
    main()
