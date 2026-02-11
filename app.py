import streamlit as st
import pandas as pd
from rapidfuzz import process, fuzz
from geopy.geocoders import Nominatim
from geopy.distance import geodesic
from dateutil import parser
import time
from typing import Optional, Tuple, List, Dict

st.set_page_config(page_title="Blocos Próximos 🎭", layout="centered")

# ============== CONFIG ==============
CSV_PATH = "blocos.csv"   # mantenha seu arquivo CSV com esse nome no repositório
TOP_K = 10
GEOCODE_SLEEP = 1.0  # segundos entre requisições ao Nominatim (respeitar rate-limit)
# =====================================

# ---------- Helpers / Caches ----------
@st.cache_resource
def get_geocoder():
    # user_agent customizado
    return Nominatim(user_agent="saopaulo_carnaval_app")

def ensure_sao_paulo_context(address: str) -> str:
    """
    Se o endereço não mencionar explicitamente 'São Paulo' ou 'SP',
    acrescenta ', São Paulo, SP, Brasil' para tornar a busca mais precisa.
    """
    if not address or str(address).strip() == "":
        return ""
    a = str(address).strip()
    a_low = a.lower()
    keywords = ["são paulo", "sao paulo", "saopaulo", ", sp", ", sp.", " sp,"]  # termos que indicam cidade/estado
    if any(k in a_low for k in keywords):
        return a
    # se o endereço já contém país ou outra cidade mas usuário informou que TODOS são de SP, nós acrescentamos SP para precisão
    return f"{a}, São Paulo, SP, Brasil"

@st.cache_data
def geocode_address(address: str) -> Optional[Tuple[float, float]]:
    """
    Geocodifica um endereço (string) e retorna (lat, lon) ou None.
    - Acrescenta automaticamente contexto de 'São Paulo, SP, Brasil' quando apropriado.
    - Restringe por country_codes='br' e pede language='pt' para melhorar correspondência local.
    """
    if not address or str(address).strip() == "":
        return None
    addr = ensure_sao_paulo_context(address)
    geolocator = get_geocoder()
    try:
        # busca principal já com contexto de São Paulo
        loc = geolocator.geocode(addr, timeout=10, country_codes="br", language="pt")
        time.sleep(GEOCODE_SLEEP)
        if loc:
            return (loc.latitude, loc.longitude)
        # fallback adicional: tentar somente com ", Brasil" caso o anterior falhe
        loc2 = geolocator.geocode(f"{address}, Brasil", timeout=10, country_codes="br", language="pt")
        time.sleep(GEOCODE_SLEEP)
        if loc2:
            return (loc2.latitude, loc2.longitude)
    except Exception:
        return None
    return None

@st.cache_data
def geocode_many(addresses: List[str]) -> Dict[str, Optional[Tuple[float, float]]]:
    """
    Recebe lista de endereços (strings), retorna dict address -> (lat, lon) ou None.
    Usa ensure_sao_paulo_context para tornar buscas mais precisas em SP.
    """
    geolocator = get_geocoder()
    results: Dict[str, Optional[Tuple[float, float]]] = {}
    unique_addrs = [a for a in sorted(set([str(x).strip() for x in addresses if str(x).strip() != ""]))]
    for addr in unique_addrs:
        addr_ctx = ensure_sao_paulo_context(addr)
        try:
            loc = geolocator.geocode(addr_ctx, timeout=10, country_codes="br", language="pt")
            time.sleep(GEOCODE_SLEEP)
            if loc:
                results[addr] = (loc.latitude, loc.longitude)
                continue
            # fallback: tentar com apenas ", Brasil"
            loc2 = geolocator.geocode(f"{addr}, Brasil", timeout=10, country_codes="br", language="pt")
            time.sleep(GEOCODE_SLEEP)
            if loc2:
                results[addr] = (loc2.latitude, loc2.longitude)
            else:
                results[addr] = None
        except Exception:
            results[addr] = None
    return results

def try_parse_time(s):
    """Tenta parsear horário (ex: '10:00', '10h', '10:30', '10:00 - 12:00'). Retorna pd.Timestamp ou None."""
    if pd.isna(s):
        return None
    s = str(s).strip()
    if not s:
        return None
    if "-" in s:
        s = s.split("-")[0].strip()
    s = s.replace("h", ":00") if s.endswith("h") and ":" not in s else s
    try:
        dt = parser.parse(s, dayfirst=True, fuzzy=True)
        return pd.Timestamp(dt)
    except Exception:
        return None

def normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    col_map = {c.lower().strip(): c for c in df.columns}
    def get_col(*possibles):
        for p in possibles:
            key = p.lower().strip()
            if key in col_map:
                return col_map[key]
        return None

    mapping = {}
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
    for col in ["Data", "Bloco", "Horário", "Bairro", "Partida", "Dispersao"]:
        if col in df.columns:
            df[col] = df[col].astype(str).str.strip()
        else:
            df[col] = ""

    df["Horario_dt"] = df["Horário"].apply(try_parse_time)
    df["Data_str"] = df["Data"].astype(str).str.strip()
    return df

# utilitários de rota (usam colunas geocodificadas se disponíveis)
def coords_for_row_field(row: pd.Series, field: str) -> Optional[Tuple[float,float]]:
    lat_col = f"{field}_lat"
    lon_col = f"{field}_lon"
    lat = row.get(lat_col) if lat_col in row.index else None
    lon = row.get(lon_col) if lon_col in row.index else None
    if pd.notna(lat) and pd.notna(lon):
        return (float(lat), float(lon))
    return geocode_address(row.get(field, ""))

def build_instance_options(df: pd.DataFrame, bloco_name: str) -> List[Tuple[int, str]]:
    rows = df[df["Bloco"] == bloco_name]
    opts = []
    for idx, r in rows.iterrows():
        label = f"{r['Data_str']} | {r['Horário']} | {r.get('Bairro','')} | {r.get('Partida','')}"
        opts.append((int(idx), label))
    return opts

def get_row_by_index(df: pd.DataFrame, idx: int) -> pd.Series:
    return df.loc[idx]

def find_candidates_from_origin(df: pd.DataFrame, origem: Tuple[float,float],
                                data_ref: str, horario_ref: pd.Timestamp,
                                exclude_idxs: List[int]=[]) -> pd.DataFrame:
    candidatos = []
    for idx, row in df.iterrows():
        if idx in exclude_idxs:
            continue
        if str(row["Data_str"]).strip() != str(data_ref).strip():
            continue
        if pd.isna(row["Horario_dt"]) or row["Horario_dt"] is None:
            continue
        try:
            if row["Horario_dt"].time() < horario_ref.time():
                continue
        except Exception:
            continue
        destino = None
        if pd.notna(row.get("Partida_lat")) and pd.notna(row.get("Partida_lon")):
            destino = (float(row["Partida_lat"]), float(row["Partida_lon"]))
        else:
            destino = geocode_address(row["Partida"])
        if not destino:
            continue
        try:
            dist_km = geodesic(origem, destino).km
        except Exception:
            continue
        candidatos.append({
            "idx": idx,
            "Bloco": row["Bloco"],
            "Data": row["Data_str"],
            "Horário": row["Horário"],
            "Bairro": row.get("Bairro", ""),
            "Partida": row["Partida"],
            "Distância_km": round(dist_km, 3)
        })
    if not candidatos:
        return pd.DataFrame(columns=["idx","Bloco","Data","Horário","Bairro","Partida","Distância_km"])
    return pd.DataFrame(candidatos).sort_values("Distância_km")

# ---------- UI ----------
st.title("🎭 Blocos Próximos (rota com até 3 blocos) — foco: São Paulo")

st.markdown("""
Escolha até 3 blocos (primeiro obrigatório).  
Os campos são *selectboxes* pesquisáveis — digite para filtrar.  
Deixe o segundo e/ou terceiro vazios para que o sistema **sugira** automaticamente as melhores opções (considerando São Paulo).
""")

# carregar dados
try:
    df_raw = pd.read_csv(CSV_PATH, sep=";", encoding="utf-8")
except FileNotFoundError:
    st.error(f"Arquivo '{CSV_PATH}' não encontrado. Faça upload no repositório ou verifique o nome.")
    st.stop()
except Exception as e:
    st.error(f"Erro ao ler '{CSV_PATH}': {e}")
    st.stop()

try:
    df = normalize_columns(df_raw)
except Exception as e:
    st.error(f"Erro ao processar colunas do CSV: {e}")
    st.stop()

# --- Pré-geocoding de Partida e Dispersao (colunas novas) com contexto São Paulo
addrs_to_geocode = list(df["Dispersao"].fillna("").unique()) + list(df["Partida"].fillna("").unique())
with st.spinner("Pré-geocodificando endereços (contexto: São Paulo) — pode demorar na primeira vez..."):
    geocode_map = geocode_many(addrs_to_geocode)

def lookup_lat(addr):
    if not addr or str(addr).strip() == "":
        return None
    v = geocode_map.get(addr.strip())
    return v[0] if v else None

def lookup_lon(addr):
    if not addr or str(addr).strip() == "":
        return None
    v = geocode_map.get(addr.strip())
    return v[1] if v else None

df["Dispersao_lat"] = df["Dispersao"].apply(lookup_lat)
df["Dispersao_lon"] = df["Dispersao"].apply(lookup_lon)
df["Partida_lat"] = df["Partida"].apply(lookup_lat)
df["Partida_lon"] = df["Partida"].apply(lookup_lon)

# reportar endereços sem coords
missing_disp = df[df["Dispersao"].isna() | df["Dispersao_lat"].isna()][["Bloco","Data_str","Horário","Dispersao"]]
missing_part = df[df["Partida"].isna() | df["Partida_lat"].isna()][["Bloco","Data_str","Horário","Partida"]]

if not missing_disp.empty:
    st.info(f"Atenção: {len(missing_disp)} dispersões sem coordenadas (ex.:). Revise os textos no CSV para incluir bairro/rua e 'São Paulo' quando possível.")
    st.dataframe(missing_disp.head(10), use_container_width=True)
if not missing_part.empty:
    st.info(f"Atenção: {len(missing_part)} partidas sem coordenadas (ex.:).")
    st.dataframe(missing_part.head(10), use_container_width=True)

# lista única de nomes (ordenada)
nomes_unicos = sorted(df["Bloco"].dropna().unique().tolist())
nomes_options = [""] + nomes_unicos

col1, col2, col3 = st.columns(3)
with col1:
    sel1_name = st.selectbox("1) Primeiro bloquinho (obrigatório)", nomes_options, index=0, help="Escolha o primeiro bloco — obrigatório.")
with col2:
    sel2_name = st.selectbox("2) Segundo bloquinho (opcional)", nomes_options, index=0, help="Opcional — deixe em branco para sugestão.")
with col3:
    sel3_name = st.selectbox("3) Último bloquinho (opcional)", nomes_options, index=0, help="Opcional — deixe em branco para sugestão.")

if not sel1_name:
    st.info("Escolha o primeiro bloquinho (campo 1) para começar a busca.")
    st.stop()

st.markdown("### Detalhe da ocorrência de cada bloco (quando houver mais de uma entrada com o mesmo nome)")
def select_occurrence_for(name: str, col_label: str) -> Optional[int]:
    opts = build_instance_options(df, name)
    if not opts:
        st.error(f"Nenhuma ocorrência encontrada para o bloco '{name}'. Verifique seu CSV.")
        return None
    if len(opts) == 1:
        idx = opts[0][0]
        st.write(f"{col_label}: **{name}** — {opts[0][1]}")
        return idx
    else:
        labels = [f"{i} | {lab}" for (i, lab) in opts]
        choice = st.selectbox(f"{col_label} — escolha a ocorrência exata", [""] + labels)
        if not choice:
            st.info(f"Escolha a ocorrência exata para '{name}' (campo '{col_label}').")
            return None
        idx_selected = int(choice.split("|")[0].strip())
        return idx_selected

idx1 = select_occurrence_for(sel1_name, "Primeiro (1)")
if idx1 is None:
    st.stop()

idx2 = None
if sel2_name:
    idx2 = select_occurrence_for(sel2_name, "Segundo (2)")
    if idx2 is None:
        st.stop()

idx3 = None
if sel3_name:
    idx3 = select_occurrence_for(sel3_name, "Último (3)")
    if idx3 is None:
        st.stop()

# carregar linhas selecionadas
row1 = get_row_by_index(df, idx1)
hora1 = row1["Horario_dt"]
data1 = row1["Data_str"]
st.subheader("Bloco 1 selecionado")
st.write(f"**Bloco:** {row1['Bloco']}")
st.write(f"**Data:** {data1}")
st.write(f"**Horário:** {row1['Horário']}")
st.write(f"**Partida:** {row1['Partida']}")
st.write(f"**Dispersão:** {row1['Dispersao']}")

if pd.isna(hora1) or hora1 is None:
    st.error("Horário do primeiro bloco não pôde ser interpretado. É necessário um horário legível.")
    st.stop()

origem1 = coords_for_row_field(row1, "Dispersao")
if not origem1:
    origem1 = coords_for_row_field(row1, "Partida")
    if origem1:
        st.warning("Não foi possível geocodificar a *Dispersão*, usando a *Partida* do 1º bloco como origem (fallback).")
    else:
        st.error("Não foi possível geocodificar a Dispersão (nem a Partida) do primeiro bloco. Sem coordenadas não é possível calcular rotas.")
        st.stop()

# (mantém a lógica dos casos A/B/C/D exatamente como nas versões anteriores,
#  usando as funções find_candidates_from_origin / coords_for_row_field etc.)

# Caso A: somente primeiro informado (sel2_name e sel3_name vazios)
if not sel2_name and not sel3_name:
    st.info("Somente o primeiro bloco foi informado — sugerindo blocos próximos como 2º e 3º.")
    candidatos2 = find_candidates_from_origin(df, origem1, data1, hora1, exclude_idxs=[idx1]).head(TOP_K)
    if candidatos2.empty:
        st.info("Nenhum candidato encontrado para o segundo bloco com base no primeiro (verifique geocoding/horários).")
    else:
        st.subheader("Sugestões para o 2º bloquinho (ordenadas por proximidade da dispersão do 1º → partida do candidato)")
        st.dataframe(candidatos2.reset_index(drop=True)[["Bloco","Data","Horário","Bairro","Partida","Distância_km"]], use_container_width=True)

        st.subheader("Para cada sugestão de 2º, sugerimos também um 3º próximo da dispersão do 2º")
        rows_for_third = []
        for _, cand in candidatos2.head(5).iterrows():
            idx_cand2 = int(cand["idx"])
            row_cand2 = get_row_by_index(df, idx_cand2)
            origem2 = coords_for_row_field(row_cand2, "Dispersao")
            if not origem2:
                continue
            hora2 = row_cand2["Horario_dt"]
            cand3_df = find_candidates_from_origin(df, origem2, row_cand2["Data_str"], hora2, exclude_idxs=[idx1, idx_cand2])
            if cand3_df.empty:
                continue
            best3 = cand3_df.iloc[0]
            rows_for_third.append({
                "2º Bloco": row_cand2["Bloco"],
                "2º Horário": row_cand2["Horário"],
                "3º Sugerido": best3["Bloco"],
                "3º Horário": best3["Horário"],
                "Distância 2->3 (km)": best3["Distância_km"],
                "Distância 1->2 (km)": cand["Distância_km"]
            })
        if rows_for_third:
            st.dataframe(pd.DataFrame(rows_for_third), use_container_width=True)
        else:
            st.info("Não foi possível sugerir 3º bloco para as principais sugestões de 2º (falta de geocoding/horários).")

# Caso B
elif sel2_name and not sel3_name:
    st.subheader("Você informou o 1º e o 2º — calculando o melhor 3º bloco (mais próximo da dispersão do 2º)")
    row2 = get_row_by_index(df, idx2)
    st.write("**Segundo (2)**")
    st.write(f"Bloco: {row2['Bloco']} — Data: {row2['Data_str']} — Horário: {row2['Horário']}")
    if pd.isna(row2["Horario_dt"]) or row2["Horario_dt"] is None:
        st.error("Horário do segundo bloco não pôde ser interpretado. O algoritmo exige horário legível.")
    else:
        origem2 = coords_for_row_field(row2, "Dispersao")
        if not origem2:
            st.error("Não foi possível geocodificar a Dispersão do 2º bloco — não é possível calcular o 3º.")
        else:
            cand3_df = find_candidates_from_origin(df, origem2, row2["Data_str"], row2["Horario_dt"], exclude_idxs=[idx1, idx2])
            if cand3_df.empty:
                st.info("Nenhum candidato encontrado para o 3º bloco com base no 2º.")
            else:
                best3 = cand3_df.iloc[0]
                st.subheader("Sugestão automática para 3º (o mais próximo da dispersão do 2º)")
                st.write(f"**Bloco:** {best3['Bloco']}")
                st.write(f"**Data:** {best3['Data']}")
                st.write(f"**Horário:** {best3['Horário']}")
                st.write(f"**Partida:** {best3['Partida']}")
                st.write(f"**Distância (km):** {best3['Distância_km']}")

# Caso C
elif sel3_name and not sel2_name:
    st.subheader("Você informou o 1º e o 3º — sugerindo opções para o 2º entre eles")
    row3 = get_row_by_index(df, idx3)
    st.write("**Último (3)**")
    st.write(f"Bloco: {row3['Bloco']} — Data: {row3['Data_str']} — Horário: {row3['Horário']}")
    if row3["Data_str"] != data1:
        st.warning("O 1º e o 3º selecionados têm datas diferentes. A sugestão de 2º será limitada a blocos com DATA IGUAL ao do 1º.")
    hora3 = row3["Horario_dt"]
    hora_min = hora1
    hora_max = None
    if pd.notna(hora3):
        hora_max = hora3
    candidatos2 = []
    destino_partida_3 = coords_for_row_field(row3, "Partida")
    for idx, row in df.iterrows():
        if idx in [idx1, idx3]:
            continue
        if str(row["Data_str"]).strip() != str(data1).strip():
            continue
        if pd.isna(row["Horario_dt"]) or row["Horario_dt"] is None:
            continue
        try:
            if row["Horario_dt"].time() < hora_min.time():
                continue
            if hora_max and row["Horario_dt"].time() > hora_max.time():
                continue
        except Exception:
            continue
        destino_partida = coords_for_row_field(row, "Partida")
        if not destino_partida:
            continue
        try:
            dist_1_to_cand = geodesic(origem1, destino_partida).km
        except Exception:
            continue
        origem_cand_disp = coords_for_row_field(row, "Dispersao")
        if origem_cand_disp and destino_partida_3:
            try:
                dist_cand_to_3 = geodesic(origem_cand_disp, destino_partida_3).km
            except Exception:
                dist_cand_to_3 = None
        else:
            dist_cand_to_3 = None
        candidatos2.append({
            "idx": idx,
            "Bloco": row["Bloco"],
            "Horário": row["Horário"],
            "Partida": row["Partida"],
            "Distância 1->2 (km)": round(dist_1_to_cand, 3),
            "Distância 2->3 (km)": round(dist_cand_to_3, 3) if dist_cand_to_3 is not None else None
        })
    if not candidatos2:
        st.info("Nenhum candidato para 2º bloco encontrado com as restrições definidas.")
    else:
        cand2_df = pd.DataFrame(candidatos2)
        def sort_key(row):
            if pd.notna(row.get("Distância 2->3 (km)")):
                return row["Distância 1->2 (km)"] + row["Distância 2->3 (km)"]
            return row["Distância 1->2 (km)"]
        cand2_df["sort_val"] = cand2_df.apply(sort_key, axis=1)
        cand2_df = cand2_df.sort_values("sort_val").head(TOP_K).drop(columns=["sort_val"])
        st.subheader("Sugestões para o 2º (ordenadas por proximidade e compatibilidade com o 3º)")
        st.dataframe(cand2_df.reset_index(drop=True), use_container_width=True)

# Caso D
else:
    st.subheader("Rota completa informada (1º, 2º e 3º)")
    row2 = get_row_by_index(df, idx2)
    row3 = get_row_by_index(df, idx3)
    st.write("**Resumo**")
    st.write(f"1) {row1['Bloco']} — {row1['Data_str']} — {row1['Horário']}")
    st.write(f"2) {row2['Bloco']} — {row2['Data_str']} — {row2['Horário']}")
    st.write(f"3) {row3['Bloco']} — {row3['Data_str']} — {row3['Horário']}")
    if row2["Data_str"] != row1["Data_str"] or row3["Data_str"] != row1["Data_str"]:
        st.warning("Os blocos não estão todos no mesmo dia — verifique a sequência desejada.")
    if pd.notna(row2["Horario_dt"]) and pd.notna(row1["Horario_dt"]) and row2["Horario_dt"].time() < row1["Horario_dt"].time():
        st.warning("O horário do 2º é anterior ao do 1º — verifique a ordem.")
    if pd.notna(row3["Horario_dt"]) and pd.notna(row2["Horario_dt"]) and row3["Horario_dt"].time() < row2["Horario_dt"].time():
        st.warning("O horário do 3º é anterior ao do 2º — verifique a ordem.")
    origem1 = coords_for_row_field(row1, "Dispersao")
    destino2 = coords_for_row_field(row2, "Partida")
    origem2 = coords_for_row_field(row2, "Dispersao")
    destino3 = coords_for_row_field(row3, "Partida")
    if origem1 and destino2:
        try:
            d12 = round(geodesic(origem1, destino2).km, 3)
            st.write(f"Distância 1→2 (km): {d12}")
        except Exception:
            pass
    if origem2 and destino3:
        try:
            d23 = round(geodesic(origem2, destino3).km, 3)
            st.write(f"Distância 2→3 (km): {d23}")
        except Exception:
            pass

st.markdown("---")
st.caption("Se quiser, copie manualmente ou ajuste o código para criar um CSV com a rota sugerida/selecionada.")
