# app.py
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
CSV_PATH = "blocos.csv"
TOP_K = 10
GEOCODE_SLEEP = 1.0  # segundos entre requisições ao Nominatim
# Coordenada de fallback (centro aproximado de São Paulo)
SAO_PAULO_CENTER = (-23.55052, -46.633308)
# =====================================

# ---------- Helpers / Caches ----------
@st.cache_resource
def get_geocoder():
    return Nominatim(user_agent="saopaulo_carnaval_app")

def ensure_sao_paulo_context(address: str) -> str:
    if not address or str(address).strip() == "":
        return ""
    a = str(address).strip()
    a_low = a.lower()
    keywords = ["são paulo", "sao paulo", "saopaulo", ", sp", ", sp.", " sp,"]
    if any(k in a_low for k in keywords):
        return a
    return f"{a}, São Paulo, SP, Brasil"

@st.cache_data
def geocode_address(address: str) -> Optional[Tuple[float, float]]:
    if not address or str(address).strip() == "":
        return None
    addr = ensure_sao_paulo_context(address)
    geolocator = get_geocoder()
    try:
        loc = geolocator.geocode(addr, timeout=10, country_codes="br", language="pt")
        time.sleep(GEOCODE_SLEEP)
        if loc:
            return (loc.latitude, loc.longitude)
        loc2 = geolocator.geocode(f"{address}, Brasil", timeout=10, country_codes="br", language="pt")
        time.sleep(GEOCODE_SLEEP)
        if loc2:
            return (loc2.latitude, loc2.longitude)
    except Exception:
        return None
    return None

@st.cache_data
def geocode_many(addresses: List[str]) -> Dict[str, Optional[Tuple[float, float]]]:
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

# ---------- aproximações/heurísticas ----------
def coords_for_row_field(row: pd.Series, field: str) -> Optional[Tuple[float,float]]:
    """
    Retorna coordenadas reais se disponíveis; caso contrário retorna None.
    (não faz heurísticas aqui; heurísticas feitas em approximate_coords)
    """
    lat_col = f"{field}_lat"
    lon_col = f"{field}_lon"
    lat = row.get(lat_col) if lat_col in row.index else None
    lon = row.get(lon_col) if lon_col in row.index else None
    if pd.notna(lat) and pd.notna(lon):
        return (float(lat), float(lon))
    # fallback: tentar geocode direto do texto
    return geocode_address(row.get(field, ""))

def approximate_coords(df: pd.DataFrame, row: pd.Series, field: str, verbose=False) -> Tuple[float,float]:
    """
    Estratégia de fallback para obter coordenadas aproximadas:
    1) coords_for_row_field (pre-geocoded / geocode_address)
    2) média de coords de outras linhas com o mesmo Bairro
    3) fuzzy match por texto (Partida/Dispersao) usando outras linhas com coords
    4) fallback SAO_PAULO_CENTER
    Retorna sempre uma tupla (lat, lon).
    """
    # 1) tentativa direta
    c = coords_for_row_field(row, field)
    if c:
        return c

    # 2) bairro: média de coords de linhas com mesmo bairro
    bairro = str(row.get("Bairro", "")).strip()
    if bairro:
        others = df[(df["Bairro"].astype(str).str.strip().str.lower() == bairro.lower())].copy()
        lats, lons = [], []
        for _, r in others.iterrows():
            lat = r.get(f"{field}_lat") if f"{field}_lat" in r.index else None
            lon = r.get(f"{field}_lon") if f"{field}_lon" in r.index else None
            if pd.notna(lat) and pd.notna(lon):
                lats.append(float(lat)); lons.append(float(lon))
            else:
                # try using the other col (dispersao/partida) if available
                other_coords = coords_for_row_field(r, field)
                if other_coords:
                    lats.append(other_coords[0]); lons.append(other_coords[1])
        if lats and lons:
            if verbose:
                st.info(f"Usando média de {len(lats)} coordenadas do bairro '{bairro}' como aproximação para campo '{field}'.")
            return (sum(lats)/len(lats), sum(lons)/len(lons))

    # 3) fuzzy match textual: procurar outro registro com texto parecido em Partida/Dispersao que tenha coords
    target_text = str(row.get(field, "")).strip()
    if target_text:
        # construir lista de candidatos textuais com coords
        candidates = []
        for idx, r in df.iterrows():
            txt = str(r.get("Partida","")) + " | " + str(r.get("Dispersao",""))
            # preferir apenas registros que possuem coords em alguma coluna
            has_coords = False
            if pd.notna(r.get("Partida_lat")) and pd.notna(r.get("Partida_lon")):
                has_coords = True
            if pd.notna(r.get("Dispersao_lat")) and pd.notna(r.get("Dispersao_lon")):
                has_coords = True
            if not has_coords:
                continue
            candidates.append((idx, txt, r))
        if candidates:
            choices = [c[1] for c in candidates]
            match = process.extractOne(target_text, choices, scorer=fuzz.WRatio)
            if match and match[1] >= 60:  # limiar razoável
                matched_idx = choices.index(match[0])
                matched_row = candidates[matched_idx][2]
                # prefer Partida coords, depois Dispersao coords, depois geocode
                if pd.notna(matched_row.get("Partida_lat")) and pd.notna(matched_row.get("Partida_lon")):
                    if verbose:
                        st.info(f"Aproximação por texto: usando coords de 'Partida' de registro similar (score {match[1]}).")
                    return (float(matched_row["Partida_lat"]), float(matched_row["Partida_lon"]))
                if pd.notna(matched_row.get("Dispersao_lat")) and pd.notna(matched_row.get("Dispersao_lon")):
                    if verbose:
                        st.info(f"Aproximação por texto: usando coords de 'Dispersao' de registro similar (score {match[1]}).")
                    return (float(matched_row["Dispersao_lat"]), float(matched_row["Dispersao_lon"]))
                # último recurso: tentar geocode do matched row
                try_coords = coords_for_row_field(matched_row, "Partida") or coords_for_row_field(matched_row, "Dispersao")
                if try_coords:
                    if verbose:
                        st.info(f"Aproximação por texto: geocoding do registro similar deu coords (score {match[1]}).")
                    return try_coords

    # 4) Fallback final: centro de São Paulo
    if verbose:
        st.warning(f"Usando fallback central de São Paulo para campo '{field}' do bloco '{row.get('Bloco','')}'. Resultado aproximado.")
    return SAO_PAULO_CENTER

# ---------- utilitários originais (ajustados para usar approximate_coords) ----------
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
        # tentar coordenadas da partida: preferir pre-geocoded, senão approximate_coords
        destino = None
        if pd.notna(row.get("Partida_lat")) and pd.notna(row.get("Partida_lon")):
            destino = (float(row["Partida_lat"]), float(row["Partida_lon"]))
        else:
            destino = approximate_coords(df, row, "Partida")
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
st.title("🎭 Blocos Próximos (rota com até 3 blocos) — aproximação robusta SP")

st.markdown("""
Escolha até 3 blocos (primeiro obrigatório).  
O app agora usa heurísticas aproximadas quando não encontra coords exatas:
- média de coordenadas do mesmo bairro
- busca por texto similar no CSV
- como último recurso, usa o centro de São Paulo (aproximação).
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

# Pré-geocoding (ainda útil): tenta geocodificar endereços únicos e preenche colunas _lat/_lon quando possível
addrs_to_geocode = list(df["Dispersao"].fillna("").unique()) + list(df["Partida"].fillna("").unique())
with st.spinner("Pré-geocodificando endereços (contexto: São Paulo) — pode demorar..."):
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

# reportar endereços sem coords pre-geocoded (apenas informação)
missing_disp = df[df["Dispersao"].isna() | df["Dispersao_lat"].isna()][["Bloco","Data_str","Horário","Dispersao"]]
missing_part = df[df["Partida"].isna() | df["Partida_lat"].isna()][["Bloco","Data_str","Horário","Partida"]]

if not missing_disp.empty:
    st.info(f"Atenção: {len(missing_disp)} dispersões sem coordenadas pre-geocoded (ex.:). O app tentará aproximar quando possível.")
    st.dataframe(missing_disp.head(10), width='stretch')
if not missing_part.empty:
    st.info(f"Atenção: {len(missing_part)} partidas sem coordenadas pre-geocoded (ex.:).")
    st.dataframe(missing_part.head(10), width='stretch')

# UI: selectboxes
nomes_unicos = sorted(df["Bloco"].dropna().unique().tolist())
nomes_options = [""] + nomes_unicos

col1, col2, col3 = st.columns(3)
with col1:
    sel1_name = st.selectbox("1) Primeiro bloquinho (obrigatório)", nomes_options, index=0)
with col2:
    sel2_name = st.selectbox("2) Segundo bloquinho (opcional)", nomes_options, index=0)
with col3:
    sel3_name = st.selectbox("3) Último bloquinho (opcional)", nomes_options, index=0)

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

# origem: tentar coords exatas, depois aproximação (verbose=True para mostrar avisos)
origem1 = coords_for_row_field(row1, "Dispersao") or coords_for_row_field(row1, "Partida")
if not origem1:
    origem1 = approximate_coords(df, row1, "Dispersao", verbose=True)

# Caso A: somente primeiro informado
if not sel2_name and not sel3_name:
    st.info("Somente o primeiro bloco foi informado — sugerindo blocos próximos como 2º e 3º.")
    candidatos2 = find_candidates_from_origin(df, origem1, data1, hora1, exclude_idxs=[idx1]).head(TOP_K)
    if candidatos2.empty:
        st.info("Nenhum candidato encontrado para o segundo bloco com base no primeiro (verifique dados).")
    else:
        st.subheader("Sugestões para o 2º bloquinho")
        st.dataframe(candidatos2.reset_index(drop=True)[["Bloco","Data","Horário","Bairro","Partida","Distância_km"]], width='stretch')

        st.subheader("Para cada sugestão de 2º, sugerimos também um 3º próximo da dispersão do 2º")
        rows_for_third = []
        for _, cand in candidatos2.head(5).iterrows():
            idx_cand2 = int(cand["idx"])
            row_cand2 = get_row_by_index(df, idx_cand2)
            origem2 = coords_for_row_field(row_cand2, "Dispersao") or approximate_coords(df, row_cand2, "Dispersao")
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
            st.dataframe(pd.DataFrame(rows_for_third), width='stretch')
        else:
            st.info("Não foi possível sugerir 3º bloco para as principais sugestões de 2º (falta de dados).")

# Caso B: 1º e 2º informados
elif sel2_name and not sel3_name:
    st.subheader("Você informou o 1º e o 2º — calculando o melhor 3º bloco (aproximado)")
    row2 = get_row_by_index(df, idx2)
    st.write(f"2º: {row2['Bloco']} — {row2['Data_str']} — {row2['Horário']}")
    if pd.isna(row2["Horario_dt"]) or row2["Horario_dt"] is None:
        st.error("Horário do segundo bloco não pôde ser interpretado.")
    else:
        origem2 = coords_for_row_field(row2, "Dispersao") or approximate_coords(df, row2, "Dispersao", verbose=True)
        if not origem2:
            st.error("Não foi possível definir coordenadas para o 2º bloco (mesmo por aproximação).")
        else:
            cand3_df = find_candidates_from_origin(df, origem2, row2["Data_str"], row2["Horario_dt"], exclude_idxs=[idx1, idx2])
            if cand3_df.empty:
                st.info("Nenhum candidato encontrado para o 3º bloco com base no 2º.")
            else:
                best3 = cand3_df.iloc[0]
                st.subheader("Sugestão automática para 3º (aproximada)")
                st.write(f"**Bloco:** {best3['Bloco']}")
                st.write(f"**Distância (km):** {best3['Distância_km']}")

# Caso C: 1º e 3º informados
elif sel3_name and not sel2_name:
    st.subheader("Você informou o 1º e o 3º — sugerindo opções para o 2º (aproximado)")
    row3 = get_row_by_index(df, idx3)
    if row3["Data_str"] != data1:
        st.warning("1º e 3º têm datas diferentes; sugestões serão filtradas por data do 1º.")
    hora3 = row3["Horario_dt"]
    hora_min = hora1
    hora_max = hora3 if pd.notna(hora3) else None
    candidatos2 = []
    destino_partida_3 = coords_for_row_field(row3, "Partida") or approximate_coords(df, row3, "Partida")
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
        destino_partida = coords_for_row_field(row, "Partida") or approximate_coords(df, row, "Partida")
        if not destino_partida:
            continue
        try:
            dist_1_to_cand = geodesic(origem1, destino_partida).km
        except Exception:
            continue
        origem_cand_disp = coords_for_row_field(row, "Dispersao") or approximate_coords(df, row, "Dispersao")
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
        st.info("Nenhum candidato para 2º bloco encontrado com as restrições.")
    else:
        cand2_df = pd.DataFrame(candidatos2)
        def sort_key(row):
            if pd.notna(row.get("Distância 2->3 (km)")):
                return row["Distância 1->2 (km)"] + row["Distância 2->3 (km)"]
            return row["Distância 1->2 (km)"]
        cand2_df["sort_val"] = cand2_df.apply(sort_key, axis=1)
        cand2_df = cand2_df.sort_values("sort_val").head(TOP_K).drop(columns=["sort_val"])
        st.subheader("Sugestões para o 2º (ordenadas por proximidade e compatibilidade)")
        st.dataframe(cand2_df.reset_index(drop=True), width='stretch')

# Caso D: todos informados
else:
    st.subheader("Rota completa informada (1º, 2º e 3º) — resumo e consistência")
    row2 = get_row_by_index(df, idx2)
    row3 = get_row_by_index(df, idx3)
    st.write(f"1) {row1['Bloco']} — {row1['Data_str']} — {row1['Horário']}")
    st.write(f"2) {row2['Bloco']} — {row2['Data_str']} — {row2['Horário']}")
    st.write(f"3) {row3['Bloco']} — {row3['Data_str']} — {row3['Horário']}")
    if row2["Data_str"] != row1["Data_str"] or row3["Data_str"] != row1["Data_str"]:
        st.warning("Os blocos não estão todos no mesmo dia — verifique.")
    if pd.notna(row2["Horario_dt"]) and pd.notna(row1["Horario_dt"]) and row2["Horario_dt"].time() < row1["Horario_dt"].time():
        st.warning("O horário do 2º é anterior ao do 1º.")
    if pd.notna(row3["Horario_dt"]) and pd.notna(row2["Horario_dt"]) and row3["Horario_dt"].time() < row2["Horario_dt"].time():
        st.warning("O horário do 3º é anterior ao do 2º.")
    origem1 = coords_for_row_field(row1, "Dispersao") or approximate_coords(df, row1, "Dispersao")
    destino2 = coords_for_row_field(row2, "Partida") or approximate_coords(df, row2, "Partida")
    origem2 = coords_for_row_field(row2, "Dispersao") or approximate_coords(df, row2, "Dispersao")
    destino3 = coords_for_row_field(row3, "Partida") or approximate_coords(df, row3, "Partida")
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
st.caption("Observação: sempre que ocorrer aproximação (média de bairro / texto similar / fallback à cidade), o resultado é aproximado — útil para filtrar e priorizar, mas não substitui coordenadas exatas.")
