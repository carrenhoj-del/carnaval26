import streamlit as st
import pandas as pd
from rapidfuzz import process, fuzz
from geopy.geocoders import Nominatim
from geopy.distance import geodesic
from dateutil import parser
import time

st.set_page_config(page_title="Blocos Próximos 🎭", layout="centered")

# ============== CONFIG ==============
CSV_PATH = "blocos.csv"   # mantenha seu arquivo CSV com esse nome no repositório
TOP_K = 10
GEOCODE_SLEEP = 1.0  # segundos entre requisições ao Nominatim (respeitar rate-limit)
# =====================================

# ---------- Helpers / Caches ----------
@st.cache_resource
def get_geocoder():
    return Nominatim(user_agent="blocos_carnaval_app")

@st.cache_data
def geocode_address(address: str):
    """
    Geocodifica um endereço (string) e retorna (lat, lon) ou None.
    Cacheado por Streamlit para não reprocurar várias vezes.
    """
    if not address or str(address).strip() == "":
        return None
    geolocator = get_geocoder()
    try:
        loc = geolocator.geocode(str(address), timeout=10)
        # pequeno delay para respeitar políticas públicas de uso
        time.sleep(GEOCODE_SLEEP)
        if loc:
            return (loc.latitude, loc.longitude)
    except Exception:
        return None
    return None

def try_parse_time(s):
    """Tenta parsear horário (ex: '10:00', '10h', '10:30', '10:00 - 12:00'). Retorna pd.Timestamp ou None."""
    if pd.isna(s):
        return None
    s = str(s).strip()
    if not s:
        return None
    # em caso de range '08:00 - 10:00', pegar a primeira parte
    if "-" in s:
        s = s.split("-")[0].strip()
    # remover 'h' isolado como '10h'
    s = s.replace("h", ":00") if s.endswith("h") and ":" not in s else s
    try:
        dt = parser.parse(s, dayfirst=True, fuzzy=True)
        return pd.Timestamp(dt)
    except Exception:
        return None

def normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    """
    Normaliza nomes de colunas esperadas (insensível a maiúsc/minúsc)
    e garante as colunas internas: 'Data'(string), 'Bloco', 'Horário', 'Bairro', 'Partida', 'Dispersao'
    """
    # criar mapa de colunas por versão lower stripped
    col_map = {c.lower().strip(): c for c in df.columns}
    # busca chaves esperadas
    def get_col(*possibles):
        for p in possibles:
            key = p.lower().strip()
            if key in col_map:
                return col_map[key]
        return None

    mapping = {}
    # suas colunas explicitamente: Índice; Data; Bloco; Horário; Bairro; partida; dispersao.
    c_data = get_col("data")
    c_bloco = get_col("bloco")
    c_hor = get_col("horário", "horario")
    c_bairro = get_col("bairro")
    c_part = get_col("partida", "local de partida", "partida")
    c_disp = get_col("dispersao", "dispersão", "dispersao")

    if not c_data or not c_bloco or not c_hor or not c_part or not c_disp:
        missing = []
        if not c_data: missing.append("Data")
        if not c_bloco: missing.append("Bloco")
        if not c_hor: missing.append("Horário")
        if not c_part: missing.append("partida")
        if not c_disp: missing.append("dispersao")
        raise ValueError(f"Colunas faltando no CSV. Verifique e inclua: {', '.join(missing)}")

    mapping[c_data] = "Data"
    mapping[c_bloco] = "Bloco"
    mapping[c_hor] = "Horário"
    if c_bairro:
        mapping[c_bairro] = "Bairro"
    mapping[c_part] = "Partida"
    mapping[c_disp] = "Dispersao"

    df = df.rename(columns=mapping)
    # garantir tipos string/strip
    for col in ["Data", "Bloco", "Horário", "Bairro", "Partida", "Dispersao"]:
        if col in df.columns:
            df[col] = df[col].astype(str).str.strip()
        else:
            df[col] = ""  # criar coluna vazia se não existir (defensivo)

    # parse horário para facilitar comparação (Timestamp com data genérica)
    df["Horario_dt"] = df["Horário"].apply(try_parse_time)
    # Mantemos a coluna 'Data' COMO STRING (sem parse) — comparação será por igualdade de string
    df["Data_str"] = df["Data"].astype(str).str.strip()

    return df

# ---------- UI ----------
st.title("🎭 Blocos Próximos (sem Google)")

st.markdown("Digite o nome (ou parte) do bloquinho. O app sugere o mais provável; confirme para ver os blocos próximos no **mesmo** dia e com horário de início **>=** o do bloquinho selecionado.")

# carregar dados
try:
    df_raw = pd.read_csv(CSV_PATH)
except FileNotFoundError:
    st.error(f"Arquivo '{CSV_PATH}' não encontrado. Faça upload no repositório ou verifique o nome.")
    st.stop()
except Exception as e:
    st.error(f"Erro ao ler '{CSV_PATH}': {e}")
    st.stop()

# normalizar e pré-processar
try:
    df = normalize_columns(df_raw)
except Exception as e:
    st.error(f"Erro ao processar colunas do CSV: {e}")
    st.stop()

# lista de nomes (original)
nomes_blocos = df["Bloco"].fillna("").tolist()

entrada = st.text_input("Nome do bloquinho (digite parte do nome):", value="")

if entrada:
    # fuzzy match
    match = process.extractOne(entrada, nomes_blocos, scorer=fuzz.WRatio)
    if not match:
        st.warning("Nenhuma correspondência encontrada. Tente digitar outra parte do nome.")
    else:
        # extractOne pode retornar (choice, score, idx) - ser defensivo
        nome_match = match[0]
        score = match[1] if len(match) > 1 else None
        try:
            idx_match = match[2] if len(match) > 2 else nomes_blocos.index(nome_match)
        except Exception:
            idx_match = None

        st.write(f"Correspondência sugerida: **{nome_match}** (score: {int(score)})")
        col1, col2 = st.columns(2)
        confirmar = col1.button("Sim, é esse", key="confirm")
        tentar_novamente = col2.button("Não, vou tentar outro", key="retry")

        if confirmar and idx_match is not None:
            # selecionado: mostrar informações completas do bloco
            bloco_atual = df.iloc[idx_match]
            st.subheader("Bloco selecionado")
            st.write(f"**Bloco:** {bloco_atual['Bloco']}")
            st.write(f"**Data:** {bloco_atual['Data_str']}")
            st.write(f"**Horário:** {bloco_atual['Horário']}")
            st.write(f"**Bairro:** {bloco_atual.get('Bairro', '')}")
            st.write(f"**Partida:** {bloco_atual['Partida']}")
            st.write(f"**Dispersão:** {bloco_atual['Dispersao']}")

            # validar horário e data
            if pd.isna(bloco_atual["Horario_dt"]) or bloco_atual["Horario_dt"] is None:
                st.error("Horário do bloco selecionado não pôde ser interpretado. O algoritmo exige horário legível para aplicar o filtro.")
            else:
                origem = geocode_address(bloco_atual["Dispersao"])
                if not origem:
                    st.error("Não foi possível geocodificar o ponto de dispersão do bloco selecionado. Sem coordenadas não há cálculo de distâncias.")
                else:
                    candidatos = []
                    data_ref = bloco_atual["Data_str"]
                    horario_ref = bloco_atual["Horario_dt"].time()

                    # percorrer outros blocos e aplicar filtros
                    for idx, row in df.iterrows():
                        # pular o mesmo bloco
                        if idx == idx_match:
                            continue
                        # filtro 1: mesma Data (string exatamente igual)
                        if str(row["Data_str"]).strip() != str(data_ref).strip():
                            continue
                        # filtro 2: horário conhecido e >= horário_ref
                        if pd.isna(row["Horario_dt"]) or row["Horario_dt"] is None:
                            continue
                        try:
                            if row["Horario_dt"].time() < horario_ref:
                                continue
                        except Exception:
                            continue
                        # geocodificar partida do candidato
                        destino = geocode_address(row["Partida"])
                        if not destino:
                            # pular candidato sem coordenadas
                            continue
                        # calcular distância (km)
                        try:
                            dist_km = geodesic(origem, destino).km
                        except Exception:
                            continue
                        candidatos.append({
                            "Bloco": row["Bloco"],
                            "Data": row["Data_str"],
                            "Horário": row["Horário"],
                            "Bairro": row.get("Bairro", ""),
                            "Partida": row["Partida"],
                            "Distância_km": round(dist_km, 3)
                        })

                    if not candidatos:
                        st.info("Nenhum bloco candidato encontrado após aplicar os filtros de data e horário.")
                    else:
                        resultado = pd.DataFrame(candidatos).sort_values("Distância_km").head(TOP_K)
                        st.subheader(f"Top {min(TOP_K, len(resultado))} blocos próximos (mesmo dia, horário >= selecionado)")
                        st.dataframe(resultado.reset_index(drop=True), use_container_width=True)

                        # botão de download do CSV com os resultados (opcional)
                        csv_bytes = resultado.to_csv(index=False, sep=",", encoding="utf-8").encode("utf-8")
                        st.download_button("Baixar resultados (CSV)", csv_bytes, file_name="top_blocos_proximos.csv", mime="text/csv")
        elif tentar_novamente:
            st.info("Digite outra parte do nome do bloquinho no campo acima.")
