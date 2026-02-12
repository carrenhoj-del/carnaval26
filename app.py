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
SAO_PAULO_CENTER = (-23.55052, -46.633308)
# Distância mínima aceitável (km) para não considerar "0.0" causada por fallback idêntico
MIN_ACCEPTABLE_DIST_KM = 0.01
# =====================================

# ---------- Helpers / Caches ----------
@st.cache_resource
def get_geocoder():
    return Nominatim(user_agent="saopaulo_carnaval_app_v2")

def build_address_with_bairro(address: str, bairro: str) -> str:
    """
    Combina endereço + bairro + contexto São Paulo para geocoding com maior chance de acerto.
    Se address vazio, retorna apenas bairro+contexto. Se ambos vazios retorna "".
    """
    a = str(address).strip() if address and str(address).strip() else ""
    b = str(bairro).strip() if bairro and str(bairro).strip() else ""
    if a and b:
        return f"{a}, {b}, São Paulo, SP, Brasil"
    if b:
        return f"{b}, São Paulo, SP, Brasil"
    if a:
        # acrescenta contexto SP para melhor precisão
        return f"{a}, São Paulo, SP, Brasil"
    return ""

@st.cache_data
def geocode_address_with_context(address: str, bairro: str) -> Optional[Tuple[float, float]]:
    """
    Geocodifica usando endereço + bairro + contexto São Paulo e configura country_codes/language.
    Retorna (lat, lon) ou None.
    """
    addr = build_address_with_bairro(address, bairro)
    if not addr:
        return None
    geolocator = get_geocoder()
    try:
        loc = geolocator.geocode(addr, timeout=10, country_codes="br", language="pt")
        time.sleep(GEOCODE_SLEEP)
        if loc:
            return (loc.latitude, loc.longitude)
        # fallback ainda tenta sem bairro (apenas address + Brasil)
        loc2 = geolocator.geocode(f"{address}, Brasil", timeout=10, country_codes="br", language="pt")
        time.sleep(GEOCODE_SLEEP)
        if loc2:
            return (loc2.latitude, loc2.longitude)
    except Exception:
        return None
    return None

@st.cache_data
def geocode_many_with_bairro(pairs: List[Tuple[str,str]]) -> Dict[Tuple[str,str], Optional[Tuple[float,float]]]:
    """
    Recebe lista de pares (address, bairro) e retorna map (address,bairro)->(lat,lon) ou None.
    Cacheado para acelerar execuções repetidas.
    """
    geolocator = get_geocoder()
    results: Dict[Tuple[str,str], Optional[Tuple[float,float]]] = {}
    unique_pairs = []
    seen = set()
    for a,b in pairs:
        key = (str(a).strip(), str(b).strip())
        if key not in seen:
            seen.add(key)
            unique_pairs.append(key)

    for addr, bairro in unique_pairs:
        addr_ctx = build_address_with_bairro(addr, bairro)
        if not addr_ctx:
            results[(addr,bairro)] = None
            continue
        try:
            loc = geolocator.geocode(addr_ctx, timeout=10, country_codes="br", language="pt")
            time.sleep(GEOCODE_SLEEP)
            if loc:
                results[(addr,bairro)] = (loc.latitude, loc.longitude)
                continue
            # fallback: address + Brasil
            loc2 = geolocator.geocode(f"{addr}, Brasil", timeout=10, country_codes="br", language="pt")
            time.sleep(GEOCODE_SLEEP)
            if loc2:
                results[(addr,bairro)] = (loc2.latitude, loc2.longitude)
            else:
                results[(addr,bairro)] = None
        except Exception:
            results[(addr,bairro)] = None
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

# ---------- heurísticas (melhoradas) ----------
def coords_for_row_field(row: pd.Series, field: str) -> Optional[Tuple[float,float]]:
    """
    Retorna coords se houver colunas pre-geocoded, senão tenta geocode com bairro.
    """
    lat_col = f"{field}_lat"
    lon_col = f"{field}_lon"
    lat = row.get(lat_col) if lat_col in row.index else None
    lon = row.get(lon_col) if lon_col in row.index else None
    if pd.notna(lat) and pd.notna(lon):
        return (float(lat), float(lon))
    # tentar geocode com o próprio bairro da linha
    return geocode_address_with_context(row.get(field, ""), row.get("Bairro", ""))

def approximate_coords_using_bairro_and_text(df: pd.DataFrame, row: pd.Series, field: str, verbose=False) -> Tuple[float,float]:
    """
    Estratégia refinada:
    1) tentar coords exatas (coords_for_row_field)
    2) média de coords de outros registros com MESMO bairro (preferir same field coords)
    3) fuzzy text match (Partida/Dispersao combos) entre registros com coords
    4) fallback central (último recurso)
    Retorna sempre (lat, lon).
    """
    # 1) tentativa direta
    c = coords_for_row_field(row, field)
    if c:
        return c

    # 2) média por bairro (usando preferencialmente as mesmas colunas pre-geocoded)
    bairro = str(row.get("Bairro","")).strip()
    if bairro:
        others = df[df["Bairro"].astype(str).str.strip().str.lower() == bairro.lower()]
        lats, lons = [], []
        for _, r in others.iterrows():
            lat = r.get(f"{field}_lat")
            lon = r.get(f"{field}_lon")
            if pd.notna(lat) and pd.notna(lon):
                lats.append(float(lat)); lons.append(float(lon))
            else:
                # se não houver nas mesmas colunas, tentar geocode do combo (Partida/Dispersao + bairro)
                try_coords = geocode_address_with_context(r.get(field, ""), r.get("Bairro",""))
                if try_coords:
                    lats.append(try_coords[0]); lons.append(try_coords[1])
        if lats and lons:
            if verbose:
                st.info(f"Aproximação: média de {len(lats)} coords do bairro '{bairro}' para campo '{field}'.")
            return (sum(lats)/len(lats), sum(lons)/len(lons))

    # 3) fuzzy match textual: buscar registro similar com coords
    target_text = str(row.get(field, "")).strip()
    if target_text:
        candidates = []
        for idx, r in df.iterrows():
            # formar texto-compare com Partida + Dispersao + Bairro
            txt = f"{r.get('Partida','')} | {r.get('Dispersao','')} | {r.get('Bairro','')}"
            # só considerar se existir coord pre-geocoded ou geocodable
            has_coords = False
            if pd.notna(r.get("Partida_lat")) and pd.notna(r.get("Partida_lon")):
                has_coords = True
            if pd.notna(r.get("Dispersao_lat")) and pd.notna(r.get("Dispersao_lon")):
                has_coords = True
            # tentar geocode com bairro para decidir se tem coords (evitar geocoding massivo aqui)
            if not has_coords:
                maybe = geocode_address_with_context(r.get("Partida",""), r.get("Bairro","")) or geocode_address_with_context(r.get("Dispersao",""), r.get("Bairro",""))
                if maybe:
                    has_coords = True
            if not has_coords:
                continue
            candidates.append((idx, txt, r))
        if candidates:
            choices = [c[1] for c in candidates]
            match = process.extractOne(target_text, choices, scorer=fuzz.WRatio)
            if match and match[1] >= 60:
                matched_idx = choices.index(match[0])
                matched_row = candidates[matched_idx][2]
                # prefer Partida coords
                if pd.notna(matched_row.get("Partida_lat")) and pd.notna(matched_row.get("Partida_lon")):
                    if verbose:
                        st.info(f"Aproximação textual: usando coords de 'Partida' de registro similar (score {match[1]}).")
                    return (float(matched_row["Partida_lat"]), float(matched_row["Partida_lon"]))
                if pd.notna(matched_row.get("Dispersao_lat")) and pd.notna(matched_row.get("Dispersao_lon")):
                    if verbose:
                        st.info(f"Aproximação textual: usando coords de 'Dispersao' de registro similar (score {match[1]}).")
                    return (float(matched_row["Dispersao_lat"]), float(matched_row["Dispersao_lon"]))
                # por fim tentar geocode desse registro com bairro
                try_coords = geocode_address_with_context(matched_row.get("Partida",""), matched_row.get("Bairro","")) or geocode_address_with_context(matched_row.get("Dispersao",""), matched_row.get("Bairro",""))
                if try_coords:
                    if verbose:
                        st.info(f"Aproximação textual: geocoding do registro similar deu coords (score {match[1]}).")
                    return try_coords

    # 4) fallback final
    if verbose:
        st.warning(f"Usando fallback central de São Paulo para campo '{field}' do bloco '{row.get('Bloco','')}'.")
    return SAO_PAULO_CENTER

# ---------- utilitários / lógica de filtros ----------
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
                                data_ref: str, min_allowed_time,  # min_allowed_time is a time object (datetime.time)
                                exclude_idxs: List[int]=[]) -> pd.DataFrame:
    """
    Retorna candidatos que:
     - mesmo dia (Data_str == data_ref)
     - horario >= min_allowed_time (time)
    Calcula Distância (origem -> partida candidato) usando coords pre-geocoded ou aproximação.
    Pula candidatos cuja distância é praticamente 0 (evita muitos resultados iguais por fallback).
    """
    candidatos = []
    for idx, row in df.iterrows():
        if idx in exclude_idxs:
            continue
        if str(row["Data_str"]).strip() != str(data_ref).strip():
            continue
        if pd.isna(row["Horario_dt"]) or row["Horario_dt"] is None:
            continue
        try:
            if row["Horario_dt"].time() < min_allowed_time:
                continue
        except Exception:
            continue
        # obter coords da partida (preferir pre-geocoded)
        destino = None
        if pd.notna(row.get("Partida_lat")) and pd.notna(row.get("Partida_lon")):
            destino = (float(row["Partida_lat"]), float(row["Partida_lon"]))
        else:
            destino = approximate_coords_using_bairro_and_text(df, row, "Partida")
        if not destino:
            continue
        try:
            dist_km = geodesic(origem, destino).km
        except Exception:
            continue
        # pular distâncias praticamente zero (provavelmente fallback idêntico)
        if dist_km is None or dist_km < MIN_ACCEPTABLE_DIST_KM:
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
st.title("🎭 Blocos Próximos (SP) — filtros refinados e geocoding por bairro")

st.markdown("""
Regras aplicadas:
1. Todos os blocos sugeridos devem estar **no mesmo dia** que o 1º.  
2. Todos os blocos sugeridos devem começar **pelo menos 2 horas após** a partida do 1º bloco (aplicado a 2º e 3º).  

Observação: quando o endereço não contém número, usamos `Partida/Dispersao + Bairro` para melhorar a geocodificação.
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

# --- Pré-geocoding POR COMBINAÇÃO (Partida+Baairro, Dispersao+Baairro)
pairs = []
for _, r in df.iterrows():
    pairs.append((r.get("Partida",""), r.get("Bairro","")))
    pairs.append((r.get("Dispersao",""), r.get("Bairro","")))

with st.spinner("Pré-geocodificando (endereço + bairro) — pode demorar na primeira execução..."):
    geocode_map = geocode_many_with_bairro(pairs)

def lookup_lat(addr, bairro):
    if not addr and not bairro:
        return None
    v = geocode_map.get((str(addr).strip(), str(bairro).strip()))
    return v[0] if v else None

def lookup_lon(addr, bairro):
    if not addr and not bairro:
        return None
    v = geocode_map.get((str(addr).strip(), str(bairro).strip()))
    return v[1] if v else None

# preencher colunas _lat/_lon quando possível
df["Dispersao_lat"] = df.apply(lambda r: lookup_lat(r["Dispersao"], r.get("Bairro","")), axis=1)
df["Dispersao_lon"] = df.apply(lambda r: lookup_lon(r["Dispersao"], r.get("Bairro","")), axis=1)
df["Partida_lat"] = df.apply(lambda r: lookup_lat(r["Partida"], r.get("Bairro","")), axis=1)
df["Partida_lon"] = df.apply(lambda r: lookup_lon(r["Partida"], r.get("Bairro","")), axis=1)

# informar usuário sobre quantos ainda ficaram sem coords pre-geocoded
missing_disp = df[df["Dispersao_lat"].isna()][["Bloco","Data_str","Horário","Dispersao","Bairro"]]
missing_part = df[df["Partida_lat"].isna()][["Bloco","Data_str","Horário","Partida","Bairro"]]
if not missing_disp.empty:
    st.info(f"Atenção: {len(missing_disp)} dispersões sem coords pre-geocoded (ex.:). App vai aproximar usando bairro/texto quando necessário.")
    st.dataframe(missing_disp.head(10), width='stretch')
if not missing_part.empty:
    st.info(f"Atenção: {len(missing_part)} partidas sem coords pre-geocoded (ex.:).")
    st.dataframe(missing_part.head(10), width='stretch')

# UI: selectboxes pesquisáveis
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

st.markdown("### Escolha a ocorrência exata quando um nome aparece mais de uma vez (data/horário diferentes)")
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

# carregar linha 1
row1 = get_row_by_index(df, idx1)
hora1 = row1["Horario_dt"]
data1 = row1["Data_str"]
st.subheader("Bloco 1 selecionado")
st.write(f"**Bloco:** {row1['Bloco']}")
st.write(f"**Data:** {data1}")
st.write(f"**Horário:** {row1['Horário']}")
st.write(f"**Partida:** {row1['Partida']}")
st.write(f"**Dispersão:** {row1['Dispersao']}")
st.write(f"**Bairro:** {row1.get('Bairro','')}")

if pd.isna(hora1) or hora1 is None:
    st.error("Horário do primeiro bloco não pôde ser interpretado. É necessário um horário legível.")
    st.stop()

# origem: prefer coords exatas, senão aproximação com bairro/texto
origem1 = coords_for_row_field(row1, "Dispersao") or coords_for_row_field(row1, "Partida")
if not origem1:
    origem1 = approximate_coords_using_bairro_and_text(df, row1, "Dispersao", verbose=True)

# definir tempo mínimo permitido para candidatos (2 horas após partida do 1º)
min_allowed_ts = hora1 + pd.Timedelta(hours=2)
min_allowed_time = min_allowed_ts.time()

# CASOS ---------------------------------------------------------
# Caso A: somente 1 informado => sugerir 2º e 3º
if not sel2_name and not sel3_name:
    st.info("Somente o primeiro foi informado — sugerindo 2º e 3º (aplicando filtros: mesmo dia; horário >= 1º + 2h).")
    candidatos2 = find_candidates_from_origin(df, origem1, data1, min_allowed_time, exclude_idxs=[idx1]).head(TOP_K)
    if candidatos2.empty:
        st.info("Nenhum candidato encontrado para o 2º com os filtros aplicados.")
    else:
        st.subheader("Sugestões para o 2º (ordenadas por proximidade)")
        st.dataframe(candidatos2.reset_index(drop=True)[["Bloco","Data","Horário","Bairro","Partida","Distância_km"]], width='stretch')

        # sugerir 3º com base na dispersão de cada candidato (mantendo filtro de 2h do 1º)
        st.subheader("Para cada sugestão de 2º, sugerimos um 3º próximo da dispersão do 2º")
        rows_for_third = []
        for _, cand in candidatos2.head(5).iterrows():
            idx_cand2 = int(cand["idx"])
            row_cand2 = get_row_by_index(df, idx_cand2)
            origem2 = coords_for_row_field(row_cand2, "Dispersao") or approximate_coords_using_bairro_and_text(df, row_cand2, "Dispersao")
            if not origem2:
                continue
            # candidatos para 3º: mesmo dia, horario >= min_allowed_time
            cand3_df = find_candidates_from_origin(df, origem2, row_cand2["Data_str"], min_allowed_time, exclude_idxs=[idx1, idx_cand2])
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
            st.info("Não foi possível sugerir 3º bloco para as principais sugestões de 2º (falta de dados/coords).")

# Caso B: 1º e 2º informados -> sugerir 3º (respeitando min_allowed_time do 1º)
elif sel2_name and not sel3_name:
    st.subheader("Você informou 1º e 2º — calculando o melhor 3º (mesmo dia; horário >= 1º+2h).")
    row2 = get_row_by_index(df, idx2)
    st.write(f"2º: {row2['Bloco']} — {row2['Data_str']} — {row2['Horário']} — Bairro: {row2.get('Bairro','')}")
    # checar horário do 2º também (não obrigatório que 2º >= min_allowed_time? manteremos filtro para o 3º)
    origem2 = coords_for_row_field(row2, "Dispersao") or approximate_coords_using_bairro_and_text(df, row2, "Dispersao", verbose=True)
    if not origem2:
        st.error("Não foi possível obter coordenadas (mesmo aproximadas) para a dispersão do 2º — não é possível sugerir o 3º com confiança.")
    else:
        # buscar candidatos para 3º respeitando data do 2º e min_allowed_time do 1º
        cand3_df = find_candidates_from_origin(df, origem2, row2["Data_str"], min_allowed_time, exclude_idxs=[idx1, idx2])
        if cand3_df.empty:
            st.info("Nenhum candidato para o 3º atendendo aos filtros (mesmo dia e horário >= 1º+2h).")
        else:
            best3 = cand3_df.iloc[0]
            st.subheader("Sugestão automática para 3º (melhor candidato)")
            st.write(f"**Bloco:** {best3['Bloco']} — Horário: {best3['Horário']} — Distância (km): {best3['Distância_km']}")

# Caso C: 1º e 3º informados -> sugerir 2ºs entre eles (aplicar mesma regra de 2h)
elif sel3_name and not sel2_name:
    st.subheader("Você informou 1º e 3º — sugerindo opções para o 2º (mesmo dia; horário >= 1º+2h e <= 3º).")
    row3 = get_row_by_index(df, idx3)
    st.write(f"3º: {row3['Bloco']} — {row3['Data_str']} — {row3['Horário']} — Bairro: {row3.get('Bairro','')}")
    if row3["Data_str"] != data1:
        st.warning("O 1º e o 3º têm datas diferentes. As sugestões serão filtradas pelo dia do 1º.")
    hora3 = row3["Horario_dt"]
    hora_max = hora3 if pd.notna(hora3) else None

    destino_partida_3 = coords_for_row_field(row3, "Partida") or approximate_coords_using_bairro_and_text(df, row3, "Partida")
    candidatos2 = []
    for idx, row in df.iterrows():
        if idx in [idx1, idx3]:
            continue
        if str(row["Data_str"]).strip() != str(data1).strip():
            continue
        if pd.isna(row["Horario_dt"]) or row["Horario_dt"] is None:
            continue
        try:
            if row["Horario_dt"].time() < min_allowed_time:
                continue
            if hora_max and row["Horario_dt"].time() > hora_max.time():
                continue
        except Exception:
            continue
        destino_partida = coords_for_row_field(row, "Partida") or approximate_coords_using_bairro_and_text(df, row, "Partida")
        if not destino_partida:
            continue
        try:
            dist_1_to_cand = geodesic(origem1, destino_partida).km
        except Exception:
            continue
        if dist_1_to_cand < MIN_ACCEPTABLE_DIST_KM:
            continue
        origem_cand_disp = coords_for_row_field(row, "Dispersao") or approximate_coords_using_bairro_and_text(df, row, "Dispersao")
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
        st.subheader("Sugestões para o 2º (ordenadas por proximidade/compatibilidade)")
        st.dataframe(cand2_df.reset_index(drop=True), width='stretch')

# Caso D: 1º,2º,3º informados -> resumo e verificações
else:
    st.subheader("Rota completa informada — resumo e verificações")
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
    origem1 = coords_for_row_field(row1, "Dispersao") or approximate_coords_using_bairro_and_text(df, row1, "Dispersao")
    destino2 = coords_for_row_field(row2, "Partida") or approximate_coords_using_bairro_and_text(df, row2, "Partida")
    origem2 = coords_for_row_field(row2, "Dispersao") or approximate_coords_using_bairro_and_text(df, row2, "Dispersao")
    destino3 = coords_for_row_field(row3, "Partida") or approximate_coords_using_bairro_and_text(df, row3, "Partida")
    if origem1 and destino2:
        try:
            d12 = round(geodesic(origem1, destino2).km, 3)
            if d12 >= MIN_ACCEPTABLE_DIST_KM:
                st.write(f"Distância 1→2 (km): {d12}")
            else:
                st.write("Distância 1→2 (km): <0.01 (aproximação muito próxima)")
        except Exception:
            pass
    if origem2 and destino3:
        try:
            d23 = round(geodesic(origem2, destino3).km, 3)
            if d23 >= MIN_ACCEPTABLE_DIST_KM:
                st.write(f"Distância 2→3 (km): {d23}")
            else:
                st.write("Distância 2→3 (km): <0.01 (aproximação muito próxima)")
        except Exception:
            pass

st.markdown("---")
st.caption("Filtro aplicado: mesmo dia + horário >= partida do 1º + 2h. Geocoding prioriza 'endereço + bairro' para maior precisão em SP.")
