"""
Observatorio del agua — Argelia
Estudiante: Miguel Angel Ospina Rua
Fuente: CORNARE / MARCO

Ejecutar:
    python -m streamlit run app_nivel_cornare.py
"""

from datetime import datetime
import math
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st


# ==========================================================
# CONFIGURACIÓN FIJA
# ==========================================================

ESTUDIANTE = "Miguel Angel Ospina Rua"
CODIGO_ESTACION = "8"
NOMBRE_ESTACION = "Argelia"

FECHA_DESDE = "2026-08-23"
FECHA_HASTA = "2026-08-30"
CALIDAD = 1

API_BASE = "https://marco.cornare.gov.co/api/v1/estaciones"
GEOPORTAL = "https://marco.cornare.gov.co/geoportal"

# Solución TEMPORAL al error de certificado del servicio.
# Volver a True cuando se pueda verificar correctamente.
VERIFICAR_SSL = False

VERDE = "#16735B"
AGUA = "#229C9C"
AMBAR = "#C68B32"

ZONA_HORARIA = ZoneInfo("America/Bogota")


# ==========================================================
# CONSULTA DE LA API
# ==========================================================

def pedir_json(session, url, params=None):
    """Consulta únicamente el servidor de MARCO."""

    destino = urlparse(url)
    origen = urlparse(API_BASE)

    if (
        destino.scheme != "https"
        or destino.netloc != origen.netloc
    ):
        raise ValueError("La consulta apunta a un servidor diferente de MARCO.")

    respuesta = session.get(
        url,
        params=params,
        timeout=(10, 30),
        allow_redirects=False,
        verify=VERIFICAR_SSL,
    )

    respuesta.raise_for_status()

    if respuesta.status_code != 200:
        raise ValueError(
            f"Respuesta inesperada: HTTP {respuesta.status_code}."
        )

    try:
        datos = respuesta.json()
    except ValueError as exc:
        raise ValueError(
            "MARCO no devolvió una respuesta JSON válida."
        ) from exc

    if not isinstance(datos, dict):
        raise ValueError(
            "La respuesta de MARCO no tiene el formato esperado."
        )

    return datos


@st.cache_data(ttl=900, show_spinner=False)
def consultar_niveles(codigo, desde, hasta, calidad):
    """Descarga las páginas sin presentar consultas parciales."""

    url = f"{API_BASE}/{codigo}/nivel"

    parametros = {
        "desde": desde,
        "hasta": hasta,
        "calidad": calidad,
    }

    registros = []
    visitadas = set()

    with requests.Session() as session:
        session.headers.update({
            "Accept": "application/json, text/plain, */*",
            "User-Agent": "Mozilla/5.0",
        })

        pagina = pedir_json(session, url, params=parametros)
        metadata = pagina

        for _ in range(200):
            valores = pagina.get("values")

            if not isinstance(valores, list):
                raise ValueError(
                    "La respuesta no contiene una lista llamada 'values'."
                )

            if not all(isinstance(item, dict) for item in valores):
                raise ValueError(
                    "Se recibieron registros con un formato inesperado."
                )

            registros.extend(valores)

            siguiente = pagina.get("next")

            if not siguiente:
                consultado = datetime.now(
                    ZONA_HORARIA
                ).strftime("%d/%m/%Y %H:%M")

                return registros, metadata, consultado

            if not isinstance(siguiente, str):
                raise ValueError(
                    "La dirección de la siguiente página no es válida."
                )

            siguiente_url = urljoin(url, siguiente)

            if siguiente_url in visitadas:
                raise ValueError(
                    "MARCO repitió una página. Se detuvo la descarga."
                )

            visitadas.add(siguiente_url)
            url = siguiente_url
            pagina = pedir_json(session, url)

    raise ValueError(
        "La consulta superó las 200 páginas. "
        "No se muestran resultados incompletos."
    )


# ==========================================================
# LIMPIEZA Y ANÁLISIS
# ==========================================================

def convertir_fecha(valor):
    """Convierte fechas con zona horaria a la hora de Colombia."""

    if not isinstance(valor, str):
        return pd.NaT

    try:
        fecha = pd.Timestamp(valor)

        if pd.isna(fecha):
            return pd.NaT

        if fecha.tzinfo is not None:
            fecha = fecha.tz_convert(
                "America/Bogota"
            ).tz_localize(None)

        return fecha

    except (ValueError, TypeError, OverflowError):
        return pd.NaT


def limpiar_registros(registros):
    """Usa los campos reales de MARCO: fecha y nivel."""

    df = pd.DataFrame(registros)

    if df.empty:
        return pd.DataFrame(columns=["fecha", "nivel"]), 0, 0

    if not {"fecha", "nivel"}.issubset(df.columns):
        raise ValueError(
            "Faltan las columnas 'fecha' o 'nivel'. "
            f"Columnas recibidas: {', '.join(map(str, df.columns))}"
        )

    df = df[["fecha", "nivel"]].copy()

    df["fecha"] = pd.to_datetime(
        df["fecha"].map(convertir_fecha),
        errors="coerce",
    )

    df["nivel"] = pd.to_numeric(
        df["nivel"],
        errors="coerce",
    ).replace(
        [float("inf"), -float("inf")],
        float("nan"),
    )

    validos = df.dropna(
        subset=["fecha", "nivel"]
    ).copy()

    descartados = len(df) - len(validos)

    duplicados = int(
        validos.duplicated("fecha", keep="last").sum()
    )

    validos = (
        validos
        .drop_duplicates("fecha", keep="last")
        .sort_values("fecha")
        .reset_index(drop=True)
    )

    return validos, descartados, duplicados


def analizar_calidad(df):
    """
    Índice académico basado en continuidad y valores atípicos.
    No mide calidad del agua ni riesgo de inundación.
    """

    q1 = df["nivel"].quantile(0.25)
    q3 = df["nivel"].quantile(0.75)
    iqr = q3 - q1

    limite_inferior = q1 - 1.5 * iqr
    limite_superior = q3 + 1.5 * iqr

    atipicos = (
        (df["nivel"] < limite_inferior)
        | (df["nivel"] > limite_superior)
    )

    resultado = {
        "atipicos": atipicos,
        "huecos": None,
        "indice": None,
        "frecuencia": None,
        "cobertura": None,
    }

    diferencias = df["fecha"].diff().dropna()

    positivas = diferencias[
        diferencias > pd.Timedelta(0)
    ]

    if len(positivas) < 2:
        return resultado

    moda = positivas.mode()

    if len(moda) != 1:
        return resultado

    frecuencia = moda.iloc[0]
    cocientes = positivas / frecuencia

    # Se admite una pequeña variación en los timestamps.
    es_regular = all(
        abs(float(valor) - round(float(valor))) <= 0.05
        for valor in cocientes
    )

    if not es_regular:
        return resultado

    huecos = sum(
        max(0, round(float(valor)) - 1)
        for valor in cocientes
    )

    cobertura = len(df) / (len(df) + huecos)

    indice = (
        0.70 * cobertura
        + 0.30 * (1 - atipicos.mean())
    ) * 100

    resultado.update({
        "huecos": int(huecos),
        "indice": round(float(indice), 1),
        "frecuencia": frecuencia,
        "cobertura": cobertura,
    })

    return resultado


def detectar_coordenadas(datos):
    """Busca coordenadas explícitas sin inventar una ubicación."""

    candidatos = [datos]

    for clave in ("station", "estacion", "metadata"):
        if isinstance(datos.get(clave), dict):
            candidatos.append(datos[clave])

    for item in candidatos:
        lat = next(
            (
                item[clave]
                for clave in ("lat", "latitude", "latitud")
                if clave in item
            ),
            None,
        )

        lon = next(
            (
                item[clave]
                for clave in ("lng", "lon", "longitude", "longitud")
                if clave in item
            ),
            None,
        )

        try:
            lat = float(lat)
            lon = float(lon)

            if (
                math.isfinite(lat)
                and math.isfinite(lon)
                and -90 <= lat <= 90
                and -180 <= lon <= 180
            ):
                return lat, lon

        except (TypeError, ValueError):
            continue

    return None


# ==========================================================
# ESTILOS Y GRÁFICOS
# ==========================================================

def aplicar_estilos():
    st.markdown(
        """
        <style>
        .stApp {
            background: #F3F7F1;
            color: #23473B;
        }

        .block-container {
            max-width: 1280px;
            padding-top: 2rem;
            padding-bottom: 3rem;
        }

        .hero {
            position: relative;
            overflow: hidden;
            background: linear-gradient(
                115deg,
                #113E33,
                #18664F 65%,
                #2D8980
            );
            color: white;
            padding: 42px;
            border-radius: 26px;
            margin-bottom: 24px;
        }

        .hero::after {
            content: '≈';
            position: absolute;
            right: 25px;
            top: -70px;
            font-size: 320px;
            opacity: 0.08;
            pointer-events: none;
        }

        .hero h1 {
            color: white;
            font-size: clamp(2.3rem, 5vw, 4rem);
            line-height: 1.12;
            margin: 12px 0;
        }

        .hero p {
            max-width: 660px;
            color: #DEEEE5;
            font-size: 1.05rem;
        }

        .eyebrow {
            letter-spacing: 3px;
            font-size: 12px;
            font-weight: 700;
        }

        .pill {
            display: inline-block;
            padding: 7px 13px;
            border: 1px solid #70A58E;
            border-radius: 30px;
            font-size: 12px;
            margin: 5px 6px 0 0;
            color: #EEFAF2;
        }

        [data-testid="stMetric"] {
            background: white;
            border: 1px solid #DEEBDE;
            border-radius: 18px;
            padding: 20px;
        }

        [data-testid="stMetricValue"],
        [data-testid="stMetricLabel"] {
            color: #16604A;
        }

        [data-testid="stCaptionContainer"] {
            color: #547166;
        }

        .footer {
            margin-top: 24px;
            padding-top: 18px;
            border-top: 1px solid #D7E6D7;
            font-size: 13px;
            color: #547166;
        }

        @media (max-width: 640px) {
            .hero {
                padding: 25px;
            }

            .block-container {
                padding-top: 1rem;
            }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def mostrar_grafico(figura, clave):
    figura.update_layout(
        template="plotly_white",
        height=350,
        margin=dict(l=20, r=20, t=30, b=30),
        font=dict(
            family="Arial",
            color="#24483D",
        ),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        legend=dict(
            orientation="h",
            y=-0.22,
        ),
        hoverlabel=dict(bgcolor="white"),
    )

    figura.update_xaxes(showgrid=False)

    figura.update_yaxes(
        gridcolor="#E1ECE5",
        zeroline=False,
    )

    st.plotly_chart(
        figura,
        width="stretch",
        theme=None,
        key=clave,
        config={
            "displaylogo": False,
            "scrollZoom": False,
        },
    )


# ==========================================================
# APLICACIÓN
# ==========================================================

def main():
    st.set_page_config(
        page_title="Argelia · Observatorio del agua",
        page_icon="🌿",
        layout="wide",
    )

    aplicar_estilos()

    periodo = (
        f"{pd.Timestamp(FECHA_DESDE):%d/%m/%Y}"
        f" — {pd.Timestamp(FECHA_HASTA):%d/%m/%Y}"
    )

    st.markdown(
        f"""
        <div class="hero">
            <div class="eyebrow">
                OBSERVATORIO DEL AGUA · CORNARE / MARCO
            </div>

            <h1>Argelia, al ritmo del agua.</h1>

            <p>
                Una mirada al comportamiento del nivel
                y a la continuidad de sus registros.
            </p>

            <span class="pill">
                ESTACIÓN {CODIGO_ESTACION.zfill(2)}
                · {NOMBRE_ESTACION.upper()}
            </span>

            <span class="pill">{ESTUDIANTE}</span>

            <span class="pill">{periodo}</span>
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.caption(
        "Monitoreo de nivel · Consulta automática "
        "· Filtro de calidad: 1 · Fuente: CORNARE / MARCO"
    )

    with st.spinner("Consultando las mediciones de Argelia…"):
        try:
            registros, metadata, consultado = consultar_niveles(
                CODIGO_ESTACION,
                FECHA_DESDE,
                FECHA_HASTA,
                CALIDAD,
            )

            df, descartados, duplicados = limpiar_registros(
                registros
            )

        except (requests.exceptions.RequestException, ValueError) as exc:
            st.error(
                "No fue posible completar la consulta "
                "de la estación 8 — Argelia."
            )

            st.info(
                "Revisa el detalle del error. "
                "Puedes volver a cargar la página para reintentar."
            )

            with st.expander("Detalle de la consulta", expanded=True):
                st.code(str(exc))

            st.link_button(
                "Abrir el geoportal de CORNARE ↗",
                GEOPORTAL,
            )
            return

    if df.empty:
        st.warning(
            "No hay mediciones válidas para esta estación "
            "en el período configurado."
        )

        st.caption(
            f"Registros recibidos: {len(registros)} · "
            f"Descartados: {descartados}"
        )

        st.link_button(
            "Consultar disponibilidad en MARCO ↗",
            GEOPORTAL,
        )
        return

    analisis = analizar_calidad(df)
    df["atipico_iqr"] = analisis["atipicos"]

    cantidad_atipicos = int(df["atipico_iqr"].sum())

    diario = (
        df.set_index("fecha")["nivel"]
        .resample("D")
        .agg(["mean", "min", "max", "count"])
    )

    st.caption(
        f"Datos disponibles: "
        f"{df['fecha'].min():%d/%m/%Y %H:%M} → "
        f"{df['fecha'].max():%d/%m/%Y %H:%M} "
        f"· Consulta: {consultado} (Colombia)"
    )

    st.caption(
        "Los niveles se muestran en la unidad original de la API. "
        "La unidad y el datum de referencia no están verificados."
    )

    # ------------------------------------------------------
    # MÉTRICAS
    # ------------------------------------------------------

    col1, col2, col3, col4 = st.columns(4)

    col1.metric(
        "Último nivel",
        f"{df['nivel'].iloc[-1]:.2f}",
    )

    col2.metric(
        "Nivel promedio",
        f"{df['nivel'].mean():.2f}",
    )

    col3.metric(
        "Lecturas disponibles",
        f"{len(df):,}",
    )

    texto_indice = (
        "No estimable"
        if analisis["indice"] is None
        else f"{analisis['indice']:.1f} / 100"
    )

    col4.metric(
        "Calidad estimada",
        texto_indice,
    )

    # ------------------------------------------------------
    # GRÁFICO TEMPORAL
    # ------------------------------------------------------

    st.subheader("01 / El pulso de la estación")

    with st.container(border=True):
        figura = go.Figure()

        figura.add_trace(
            go.Scatter(
                x=df["fecha"],
                y=df["nivel"],
                mode="lines" if len(df) > 1 else "markers",
                name="Nivel",
                line=dict(
                    color=VERDE,
                    width=2.5,
                ),
                marker=dict(
                    color=VERDE,
                    size=8,
                ),
            )
        )

        if cantidad_atipicos:
            atipicos = df[df["atipico_iqr"]]

            figura.add_trace(
                go.Scatter(
                    x=atipicos["fecha"],
                    y=atipicos["nivel"],
                    mode="markers",
                    name="Atípico IQR",
                    marker=dict(
                        color=AMBAR,
                        size=7,
                    ),
                )
            )

        figura.update_layout(
            xaxis_title="Fecha · hora de Colombia",
            yaxis_title="Nivel · unidad de la API",
            hovermode="x unified",
        )

        mostrar_grafico(figura, "serie_nivel")

        st.caption(
            "La línea une las observaciones disponibles. "
            "Los intervalos entre puntos pueden contener huecos."
        )

    # ------------------------------------------------------
    # BARRAS Y TORTA
    # ------------------------------------------------------

    st.subheader("02 / Los datos, desde otra perspectiva")

    izquierda, derecha = st.columns([1.4, 1])

    with izquierda:
        with st.container(border=True):
            st.markdown("**Nivel promedio por día**")

            figura = go.Figure(
                go.Bar(
                    x=diario.index,
                    y=diario["mean"],
                    marker_color=AGUA,
                    name="Promedio diario",
                    hovertemplate=(
                        "%{x|%d/%m/%Y}<br>"
                        "Promedio: %{y:.2f}"
                        "<extra></extra>"
                    ),
                )
            )

            figura.update_layout(
                xaxis_title="Día",
                yaxis_title="Nivel · unidad de la API",
            )

            mostrar_grafico(figura, "promedio_diario")

            st.caption(
                "Promedio de las lecturas disponibles por día. "
                "Los días sin mediciones quedan vacíos."
            )

    with derecha:
        with st.container(border=True):
            st.markdown("**Distribución de lecturas · torta**")

            figura = go.Figure(
                go.Pie(
                    labels=[
                        "Dentro del rango IQR",
                        "Atípicas IQR",
                    ],
                    values=[
                        len(df) - cantidad_atipicos,
                        cantidad_atipicos,
                    ],
                    hole=0.68,
                    sort=False,
                    marker=dict(
                        colors=[VERDE, AMBAR],
                    ),
                    textinfo="percent",
                    hovertemplate=(
                        "%{label}<br>"
                        "%{value} lecturas · %{percent}"
                        "<extra></extra>"
                    ),
                )
            )

            figura.add_annotation(
                text=f"<b>{len(df):,}</b><br>lecturas",
                x=0.5,
                y=0.5,
                showarrow=False,
                font_size=21,
            )

            mostrar_grafico(figura, "torta_calidad")

            st.caption(
                "Un valor atípico requiere revisión; "
                "también puede representar un evento real."
            )

    # ------------------------------------------------------
    # HALLAZGOS
    # ------------------------------------------------------

    st.subheader("03 / Hallazgos del período")

    pico = df.loc[df["nivel"].idxmax()]

    cambio = (
        df["nivel"].iloc[-1]
        - df["nivel"].iloc[0]
    )

    col1, col2, col3 = st.columns(3)

    with col1:
        with st.container(border=True):
            st.markdown("🌊 **Mayor nivel observado**")
            st.markdown(f"### {pico['nivel']:.2f}")

            st.caption(
                f"Registrado el "
                f"{pico['fecha']:%d/%m/%Y a las %H:%M}."
            )

    with col2:
        with st.container(border=True):
            st.markdown("🌱 **Cambio entre extremos**")

            if len(df) > 1:
                st.markdown(f"### {cambio:+.2f}")
            else:
                st.markdown("### No estimable")

            st.caption(
                "Última lectura menos primera lectura. "
                "No representa un pronóstico."
            )

    with col3:
        with st.container(border=True):
            st.markdown("🔎 **Lecturas para revisar**")

            st.markdown(
                f"### {cantidad_atipicos / len(df):.1%}"
            )

            st.caption(
                f"{cantidad_atipicos} de {len(df)} lecturas "
                "fuera del rango IQR."
            )

    with st.container(border=True):
        st.markdown("**Continuidad de los reportes por día**")

        figura = go.Figure(
            go.Bar(
                x=diario.index,
                y=diario["count"],
                marker_color=VERDE,
                name="Lecturas",
                hovertemplate=(
                    "%{x|%d/%m/%Y}<br>"
                    "%{y} lecturas"
                    "<extra></extra>"
                ),
            )
        )

        figura.update_layout(
            xaxis_title="Día",
            yaxis_title="Número de lecturas",
        )

        mostrar_grafico(figura, "cantidad_diaria")

    # ------------------------------------------------------
    # MAPA Y METODOLOGÍA
    # ------------------------------------------------------

    st.subheader("04 / Territorio y transparencia")

    izquierda, derecha = st.columns(2)

    with izquierda:
        with st.container(border=True):
            st.markdown("**Argelia · estación 8**")

            coordenadas = detectar_coordenadas(metadata)

            if coordenadas:
                latitud, longitud = coordenadas

                st.map(
                    pd.DataFrame({
                        "lat": [latitud],
                        "lon": [longitud],
                    }),
                    zoom=11,
                )

                st.caption(
                    "Coordenadas incluidas en la respuesta de la API."
                )

            else:
                st.info(
                    "La respuesta no incluye coordenadas "
                    "reconocibles de la estación. "
                    "Puedes consultar su ubicación en MARCO."
                )

            st.link_button(
                "Explorar el geoportal MARCO ↗",
                GEOPORTAL,
            )

    with derecha:
        with st.container(border=True):
            st.markdown("**Cómo interpretar los resultados**")

            st.write(
                "El índice académico combina un 70% de cobertura "
                "temporal estimada y un 30% de lecturas no atípicas."
            )

            st.write(
                "No representa calidad del agua "
                "ni riesgo de inundación."
            )

            st.write(
                "Los valores atípicos se detectan fuera del intervalo "
                "[Q1 − 1.5 × IQR, Q3 + 1.5 × IQR]. "
                "Se conservan en los gráficos y promedios."
            )

            if analisis["huecos"] is None:
                st.caption(
                    "Cobertura no estimable: faltan lecturas "
                    "o no se puede inferir una frecuencia regular."
                )

            else:
                st.caption(
                    f"Intervalo inferido: {analisis['frecuencia']} · "
                    f"Huecos estimados: {analisis['huecos']} · "
                    f"Cobertura: {analisis['cobertura']:.1%}."
                )

                st.caption(
                    "La cobertura se estima entre la primera "
                    "y la última lectura; no incluye los bordes "
                    "del período solicitado."
                )

            st.caption(
                f"Registros inválidos descartados: {descartados} · "
                f"Fechas duplicadas eliminadas: {duplicados}."
            )

    # ------------------------------------------------------
    # TABLA Y DESCARGA
    # ------------------------------------------------------

    with st.expander("Ver mediciones y descargar CSV"):
        st.dataframe(
            df,
            width="stretch",
            hide_index=True,
        )

        csv = df.to_csv(
            index=False
        ).encode("utf-8-sig")

        st.download_button(
            label="⬇️ Descargar mediciones CSV",
            data=csv,
            file_name="nivel_argelia_estacion_8.csv",
            mime="text/csv",
        )

    st.markdown(
        f"""
        <div class="footer">
            🌿 {ESTUDIANTE}
            · Proyecto académico
            · Fuente: CORNARE / MARCO
        </div>
        """,
        unsafe_allow_html=True,
    )


if __name__ == "__main__":
    main()
