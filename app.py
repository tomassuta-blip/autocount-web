import io
import os
import re
import zipfile
import base64
import difflib
import json
import sqlite3
import xml.etree.ElementTree as ET
import pandas as pd
import requests
import streamlit as st
from datetime import datetime
from typing import Dict, List, Any
from pypdf import PdfReader

# ==========================================
# CONFIGURACIÓN Y ESTILOS SAAS AUTOCOUNT.AI
# ==========================================
st.set_page_config(page_title="AutoCount.ai - Conector Siigo", page_icon="⚡", layout="wide")

LOGO_URL = "https://start.docuware.com/hubfs/AI%20main%20image.jpg"

st.markdown("""
<style>
    .block-container { padding-top: 3.5rem !important; padding-bottom: 0.5rem !important; max-width: 98%; }
    html, body, [class*="css"] { font-size: 0.73rem !important; font-family: 'Inter', sans-serif; }
    .stTextInput label, .stSelectbox label, .stNumberInput label { font-size: 0.68rem !important; font-weight: 700; margin-bottom: -4px !important; }
    .stTextInput input, .stSelectbox select, .stNumberInput input { font-size: 0.72rem !important; padding: 2px 4px !important; height: 26px !important; }
    .stButton>button { font-size: 0.75rem !important; font-weight: bold; padding: 0.3rem 0.8rem !important; border-radius: 6px; }
    div[data-testid="stVerticalBlock"] > div { gap: 0.2rem !important; }
    
    .autocount-header {
        background: linear-gradient(90deg, #0f172a 0%, #1e293b 100%);
        color: #ffffff;
        padding: 10px 16px;
        border-radius: 8px;
        margin-bottom: 12px;
        display: flex;
        justify-content: space-between;
        align-items: center;
        box-shadow: 0 2px 5px rgba(0,0,0,0.15);
    }
    .siigo-table-header { background-color: #f1f5f9; padding: 6px 10px; font-weight: bold; font-size: 0.72rem; color: #0f172a; border-radius: 4px; margin-bottom: 4px; }
    .badge-ok { background-color: #d1fae5; color: #065f46; padding: 2px 6px; border-radius: 4px; font-weight: bold; font-size: 0.7rem; }
    .badge-warn { background-color: #fee2e2; color: #991b1b; padding: 2px 6px; border-radius: 4px; font-weight: bold; font-size: 0.7rem; }
    .badge-danger { background-color: #fef2f2; color: #b91c1c; border: 1px solid #fca5a5; padding: 2px 6px; border-radius: 4px; font-weight: bold; font-size: 0.7rem; }
    .badge-siigo { background-color: #e0f2fe; color: #0369a1; padding: 3px 8px; border-radius: 4px; font-weight: bold; font-size: 0.78rem; }
    .badge-role { background-color: #38bdf8; color: #0f172a; padding: 2px 8px; border-radius: 12px; font-size: 0.65rem; font-weight: bold; margin-left: 8px; }
    .login-box { max-width: 400px; margin: 40px auto; padding: 25px; border: 1px solid #e2e8f0; border-radius: 10px; background-color: #ffffff; }
</style>
""", unsafe_allow_html=True)

DEFAULT_PUC = [
    "51355001 - Servicios Técnicos Exterior", "51353501 - Comisiones y Honorarios", "51352001 - Procesamiento de Datos y Software",
    "51351501 - Asistencia Técnica", "51350501 - Aseo y Vigilancia", "51354001 - Teléfono y Comunicaciones",
    "51357001 - Asesoría Jurídica y Financiera", "51359501 - Otros Servicios Diversos", "23651501 - Retención en la Fuente - Honorarios / Servicios"
]

# ==========================================
# 💾 BASE DE DATOS LOCAL PERSISTENTE (SQLite)
# ==========================================
def get_db_connection():
    return sqlite3.connect('autocount.db', check_same_thread=False)

def init_db():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('CREATE TABLE IF NOT EXISTS tenants (nit TEXT PRIMARY KEY, razon_social TEXT, siigo_user TEXT, siigo_key TEXT, puc TEXT)')
    c.execute('CREATE TABLE IF NOT EXISTS users (email TEXT PRIMARY KEY, password TEXT, nombre TEXT, tenant_nit TEXT, rol TEXT)')
    c.execute('CREATE TABLE IF NOT EXISTS docs (id TEXT PRIMARY KEY, tenant_nit TEXT, doc_ref TEXT, tipo TEXT, estado TEXT, data TEXT)')
    c.execute('CREATE TABLE IF NOT EXISTS history (id TEXT PRIMARY KEY, tenant_nit TEXT, doc_ref TEXT, tipo TEXT, fecha TEXT, total REAL, moneda TEXT, siigo_id TEXT, proveedor TEXT, nit_proveedor TEXT, pdf_b64 TEXT)')
    
    try: c.execute('ALTER TABLE history ADD COLUMN data_json TEXT')
    except: pass
    try: c.execute('ALTER TABLE history ADD COLUMN usuario TEXT')
    except: pass
    
    c.execute("SELECT COUNT(*) FROM users")
    if c.fetchone()[0] == 0:
        c.execute("INSERT INTO tenants VALUES (?,?,?,?,?)", ('900557218', 'DAVINCI TECHNOLOGIES SAS', 'tomas.suta@davinci.tech', 'MzgxZmVjOTQtMjJhMS00YTdkLWI0NjctYTdjNDRmOTQ3NWQ2OjU+PSUtcjRiNVA=', json.dumps(DEFAULT_PUC)))
        c.execute("INSERT INTO users VALUES (?,?,?,?,?)", ('tomas.suta@davinci.tech', 'admin', 'Tomás Suta', '900557218', 'SuperAdmin'))
    conn.commit()
    conn.close()

init_db()

def db_is_doc_already_processed(tenant_nit, doc_ref, tipo):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM history WHERE tenant_nit=? AND doc_ref=? AND tipo=?", (tenant_nit, str(doc_ref), tipo))
    if c.fetchone()[0] > 0:
        conn.close()
        return True, "ya fue causada en Siigo (Histórico)"
    c.execute("SELECT estado FROM docs WHERE tenant_nit=? AND doc_ref=? AND tipo=?", (tenant_nit, str(doc_ref), tipo))
    row = c.fetchone()
    conn.close()
    if row and row[0] in ['Aprobado', 'Rechazado', 'Pendiente']: return True, f"ya está registrada en estado {row[0]}"
    return False, None

def auto_clean_processed_docs(tenant_nit):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("""
        DELETE FROM docs WHERE tenant_nit=? AND id IN (
            SELECT d.id FROM docs d JOIN history h ON d.tenant_nit = h.tenant_nit AND d.doc_ref = h.doc_ref AND d.tipo = h.tipo
        )
    """, (tenant_nit,))
    conn.commit(); conn.close()

def db_auth_user(email, password):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT email, nombre, tenant_nit, rol FROM users WHERE email=? AND password=?", (email.lower().strip(), password))
    user = c.fetchone()
    conn.close()
    if user: return {"email": user[0], "nombre": user[1], "tenant_nit": user[2], "rol": user[3]}
    return None

def db_get_tenant(nit):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT nit, razon_social, siigo_user, siigo_key, puc FROM tenants WHERE nit=?", (nit,))
    row = c.fetchone()
    conn.close()
    if row: return {"nit": row[0], "razon_social": row[1], "siigo_user": row[2], "siigo_key": row[3], "puc": json.loads(row[4] or '[]')}
    return None

def db_save_doc(tenant_nit, doc_ref, tipo, estado, data_dict):
    conn = get_db_connection()
    c = conn.cursor()
    doc_id = f"{tenant_nit}_{tipo}_{doc_ref}"
    c.execute("INSERT OR REPLACE INTO docs (id, tenant_nit, doc_ref, tipo, estado, data) VALUES (?,?,?,?,?,?)",
              (doc_id, tenant_nit, doc_ref, tipo, estado, json.dumps(data_dict)))
    conn.commit(); conn.close()

def db_delete_doc(tenant_nit, doc_ref, tipo):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("DELETE FROM docs WHERE id=?", (f"{tenant_nit}_{tipo}_{doc_ref}",))
    conn.commit(); conn.close()

def db_get_docs(tenant_nit, tipo, estado=None):
    conn = get_db_connection()
    c = conn.cursor()
    if estado: c.execute("SELECT data FROM docs WHERE tenant_nit=? AND tipo=? AND estado=?", (tenant_nit, tipo, estado))
    else: c.execute("SELECT data FROM docs WHERE tenant_nit=? AND tipo=?", (tenant_nit, tipo))
    rows = c.fetchall()
    conn.close()
    return [json.loads(r[0]) for r in rows]

def db_save_history(tenant_nit, doc_ref, tipo, fecha, total, moneda, siigo_id, prov, nit_prov, pdf_b64, data_json="{}", usuario=""):
    conn = get_db_connection()
    c = conn.cursor()
    hist_id = f"{tenant_nit}_{tipo}_{doc_ref}"
    c.execute("INSERT OR REPLACE INTO history (id, tenant_nit, doc_ref, tipo, fecha, total, moneda, siigo_id, proveedor, nit_proveedor, pdf_b64, data_json, usuario) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
              (hist_id, tenant_nit, doc_ref, tipo, fecha, total, moneda, siigo_id, prov, nit_prov, pdf_b64, data_json, usuario))
    c.execute("DELETE FROM docs WHERE id=?", (hist_id,))
    conn.commit(); conn.close()

def db_get_history(tenant_nit):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT doc_ref, tipo, fecha, total, moneda, siigo_id, proveedor, nit_proveedor, pdf_b64, data_json, usuario FROM history WHERE tenant_nit=? ORDER BY fecha DESC", (tenant_nit,))
    rows = c.fetchall()
    conn.close()
    res = []
    for r in rows:
        res.append({
            "id_doc_prov": r[0], "tipo": r[1], "fecha": r[2], "total": r[3], "moneda": r[4], 
            "id_siigo_num": r[5], "proveedor": r[6], "nit": r[7], "pdf_original": base64.b64decode(r[8]) if r[8] else None,
            "data_json": r[9] if r[9] else "{}", "usuario": r[10] if r[10] else "N/A"
        })
    return res

def generar_excel(df):
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='Reporte')
    return output.getvalue()

def extraer_valores_reporte(d, h_total=None):
    is_fc = d.get("tipo_origen") == "FC" or "Resumen" in d
    
    if is_fc:
        r = d.get("Resumen", {})
        detalles = d.get("Detalle", [])
        conceptos_list = [str(i.get("Concepto", "") or i.get("description", "")) for i in detalles]
        conceptos = " | ".join([c for c in conceptos_list if c]) or "Factura de Compra"
        
        if detalles:
            subt = sum(float(i.get("Subtotal", float(i.get("Cantidad", 1)) * float(i.get("Valor Unitario", 0)))) for i in detalles)
            iva = sum(float(i.get("Valor IVA", float(i.get("Subtotal", 0)) * (float(i.get("IVA %", 0))/100.0))) for i in detalles)
            tot = subt + iva
        else:
            subt = float(r.get("Subtotal", h_total or 0))
            iva = float(r.get("IVA", 0))
            tot = float(r.get("Total", subt + iva))
        
        return {
            "tipo": "Factura Compra", "doc_ref": r.get("ID", d.get("doc_ref", "")), "fecha": r.get("Fecha", d.get("fecha", "")),
            "nit": r.get("NIT", d.get("nit", "")), "proveedor": r.get("Proveedor", d.get("proveedor", "")), "conceptos": conceptos,
            "moneda": "COP", "subtotal": round(subt, 2), "iva": round(iva, 2), "total": round(tot, 2),
            "aprobador": r.get("UsuarioAprobador", d.get("UsuarioAprobador", "N/A")), "fecha_aprobacion": r.get("FechaAprobacion", d.get("FechaAprobacion", "N/A")),
            "causador": d.get("UsuarioCausador", "N/A"), "fecha_causacion": d.get("FechaCausacion", "N/A")
        }
    else:
        items = d.get("items_custom", [])
        conceptos_list = [str(i.get("description", "")) for i in items]
        conceptos = " | ".join([c for c in conceptos_list if c]) or "Documento Soporte"
        
        if items:
            subt = sum(float(i.get("price", 0)) * float(i.get("quantity", 1)) for i in items)
            iva = sum(float(i.get("price", 0)) * float(i.get("quantity", 1)) * (float(i.get("pct_iva", 0))/100.0) for i in items)
            tot = subt + iva
        else:
            subt = float(d.get("subtotal", d.get("monto_origen", h_total or 0)))
            iva = float(d.get("iva", 0))
            tot = float(d.get("total", subt + iva))
            
        return {
            "tipo": "Doc. Soporte", "doc_ref": d.get("documento_ref", d.get("doc_ref", "")), "fecha": d.get("fecha", ""),
            "nit": d.get("nit", ""), "proveedor": d.get("proveedor", ""), "conceptos": conceptos,
            "moneda": d.get("moneda_origen", "COP"), "subtotal": round(subt, 2), "iva": round(iva, 2), "total": round(tot, 2),
            "aprobador": d.get("UsuarioAprobador", "N/A"), "fecha_aprobacion": d.get("FechaAprobacion", "N/A"),
            "causador": d.get("UsuarioCausador", "N/A"), "fecha_causacion": d.get("FechaCausacion", "N/A")
        }

# ==========================================
# 🔑 LOGIN
# ==========================================
if 'authenticated_user' not in st.session_state: st.session_state['authenticated_user'] = None

if st.session_state['authenticated_user'] is None:
    st.markdown(f"""
    <div style='text-align: center; margin-top: 30px;'>
        <img src='{LOGO_URL}' height='55' style='border-radius: 8px; margin-bottom: 8px;'>
        <div style='font-size: 1.8rem; font-weight: 800; color: #0f172a;'>AutoCount<span style='color: #38bdf8;'>.ai</span></div>
        <p style='color: #64748b; font-size: 0.85rem;'>Plataforma SaaS de Causación e Integración Contable Automática</p>
    </div>
    """, unsafe_allow_html=True)
    with st.container():
        st.markdown("<div class='login-box'>", unsafe_allow_html=True)
        with st.form("form_login"):
            st.subheader("Ingreso Seguro")
            email_in = st.text_input("Correo Electrónico")
            pass_in = st.text_input("Contraseña", type="password")
            if st.form_submit_button("🚀 Entrar a la Plataforma", type="primary", use_container_width=True):
                user_info = db_auth_user(email_in, pass_in)
                if user_info:
                    st.session_state['authenticated_user'] = user_info
                    st.toast(f"¡Bienvenido, {user_info['nombre']}!", icon="🎉")
                    st.rerun()
                else: st.error("❌ Correo o contraseña incorrectos.")
        st.markdown("</div>", unsafe_allow_html=True)
    st.stop()

# ==========================================
# 🛡️ SESIÓN Y PERMISOS DE USUARIOS
# ==========================================
curr_user = st.session_state['authenticated_user']
curr_rol = curr_user['rol']

if curr_rol == "SuperAdmin":
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT nit, razon_social FROM tenants")
    all_tenants = c.fetchall(); conn.close()
    tenant_opts = [f"{t[0]} - {t[1]}" for t in all_tenants]
    active_tenant_str = st.sidebar.selectbox("🏢 Panel SuperAdmin: Empresa Activa", tenant_opts)
    curr_tenant_nit = active_tenant_str.split(" - ")[0]
else: curr_tenant_nit = curr_user['tenant_nit']

# CORRECCIÓN 1: LIMPIEZA DE CACHÉ AL CAMBIAR DE EMPRESA
if st.session_state.get('last_tenant_active') != curr_tenant_nit:
    st.cache_data.clear()
    st.session_state['last_tenant_active'] = curr_tenant_nit

curr_tenant = db_get_tenant(curr_tenant_nit)
auto_clean_processed_docs(curr_tenant_nit)

can_upload  = curr_rol in ['SuperAdmin', 'Administrador', 'Administrativo', 'Auxiliar Administrativo']
can_approve = curr_rol in ['SuperAdmin', 'Administrador', 'Administrativo']
can_cause   = curr_rol in ['SuperAdmin', 'Administrador', 'Asistente Contable']
can_config  = curr_rol in ['SuperAdmin']
can_admin   = curr_rol in ['SuperAdmin', 'Administrador']

st.markdown(f"""
<div class="autocount-header">
    <div style="display: flex; align-items: center; gap: 12px;">
        <img src="{LOGO_URL}" height="32" style="border-radius: 6px; object-fit: contain; background: white; padding: 2px;">
        <div>
            <span style="font-size: 1.25rem; font-weight: 800; letter-spacing: -0.5px; color: #ffffff;">AutoCount<span style="color: #38bdf8;">.ai</span></span>
            <span style="font-size: 0.65rem; background-color: #334155; color: #94a3b8; padding: 2px 8px; border-radius: 12px; margin-left: 6px; font-weight: 600;">SaaS Edition</span>
        </div>
    </div>
    <div style="font-size: 0.75rem;">
        🏢 <b>{curr_tenant['razon_social']}</b> | 👤 {curr_user['nombre']} <span class='badge-role'>{curr_rol}</span>
    </div>
</div>
""", unsafe_allow_html=True)

# ==========================================
# 🔑 SIIGO API AUTHENTICATION & FUNCIONES
# ==========================================
@st.cache_data(ttl=3600)
def obtener_token_siigo(user, key):
    try:
        res = requests.post("https://api.siigo.com/auth", json={"username": user, "access_key": key}, headers={"Content-Type": "application/json"}, timeout=10)
        return (res.json().get("access_token"), None) if res.status_code == 200 else (None, f"HTTP {res.status_code}: {res.text}")
    except Exception as e: return None, str(e)

def get_siigo_headers():
    token, err = obtener_token_siigo(curr_tenant['siigo_user'], curr_tenant['siigo_key'])
    return ({"Authorization": f"Bearer {token}", "Content-Type": "application/json", "Partner-Id": "SandboxSiigo"}, None) if token else (None, err)

def crear_tercero_express_siigo(nit, nombre, apellidos="", es_empresa=True, id_type="31", email="", telefono="", direccion="", ciudad_dane="11001", resp_fiscal="R-99-PN", tipo_tercero_str="Supplier"):
    headers, err = get_siigo_headers()
    if not headers: return False, f"Error Auth: {err}"
    nit_limpio = re.sub(r'\D', '', str(nit))
    if not nit_limpio: return False, "❌ Error: NIT vacío o inválido"

    tipo_terc = "Customer" if "Cliente" in tipo_tercero_str else ("Other" if "Otro" in tipo_tercero_str else "Supplier")
    ciudad_clean = re.sub(r'\D', '', str(ciudad_dane)) or "11001"
    
    payload = {
        "type": tipo_terc, "person_type": "Company" if es_empresa else "Person", "id_type": str(id_type), "identification": nit_limpio,
        "name": [nombre] if es_empresa else [nombre, apellidos or "N/A"],
        "address": {"address": direccion or "Carrera 1 # 1-1", "city": {"country_code": "Co", "state_code": ciudad_clean[:2] if len(ciudad_clean)>=2 else "11", "city_code": ciudad_clean if len(ciudad_clean)==5 else "11001"}},
        "phones": [{"indicative": "57", "number": telefono or "3000000000"}],
        "contacts": [{"first_name": nombre[:50], "last_name": (apellidos or "Contacto")[:50], "email": email or "contacto@proveedor.com"}],
        "fiscal_responsibilities": [{"code": resp_fiscal or "R-99-PN"}]
    }

    try:
        res = requests.post("https://api.siigo.com/v1/customers", json=payload, headers=headers, timeout=10)
        if res.status_code in [200, 201]: st.cache_data.clear(); return True, f"✅ Tercero Creado con Éxito en Siigo (NIT: {nit_limpio})"
        else: return False, f"❌ Error Siigo API ({res.status_code}): {res.text}"
    except Exception as e: return False, f"❌ Error de Conexión: {e}"

@st.dialog("📝 Crear / Editar Tercero en Siigo")
def modal_formulario_tercero(nit_def, nombre_def, es_extranjero=False):
    st.caption("Verifique y ajuste los datos del tercero antes de crearlo en Siigo:")
    with st.form("form_modal_tercero"):
        col1, col2 = st.columns(2)
        with col1:
            tipo_tercero_ui = st.selectbox("Tipo de Tercero", ["Proveedor (Supplier)", "Cliente (Customer)", "Otro (Other)"])
            tipo_persona_ui = st.selectbox("Tipo de Persona", ["Empresa (Company)", "Persona Natural (Person)"], index=0 if any(k in nombre_def.upper() for k in ["S.A.S", "INC", "LLC", "LTD", "SA"]) else 1)
            id_type_ui = st.selectbox("Tipo Identificación", ["31 - NIT", "13 - Cédula de Ciudadanía", "50 - NIT Extranjero", "42 - Documento Identificación Extranjero"], index=2 if es_extranjero else (0 if "Empresa" in tipo_persona_ui else 1))
            nit_ui = st.text_input("NIT / Cédula (Solo números)", value=re.sub(r'\D', '', str(nit_def)))
        with col2:
            nombre_ui = st.text_input("Razón Social / Nombre", value=nombre_def)
            apellidos_ui = st.text_input("Apellidos (Solo Persona Natural)", value="")
            correo_ui = st.text_input("Correo Electrónico", value="contacto@proveedor.com")
            telefono_ui = st.text_input("Teléfono / Celular", value="3112289967")

        st.markdown("---")
        col3, col4 = st.columns(2)
        with col3: direccion_ui = st.text_input("Dirección", value="Calle 86 A - No 13-09")
        with col4:
            ciudad_ui = st.selectbox("Ciudad (DANE)", ["11001 - Bogotá", "05001 - Medellín", "76001 - Cali", "08001 - Barranquilla", "68001 - Bucaramanga"])
            resp_fiscal_ui = st.selectbox("Responsabilidad Fiscal", ["R-99-PN - No aplica - Otros", "O-13 - Gran contribuyente", "O-15 - Autorretenedor", "O-23 - Agente de retención IVA", "O-47 - Régimen simple de tributación", "O-48 - Impuesto sobre las ventas - IVA"])

        if st.form_submit_button("🚀 Guardar y Crear Tercero en Siigo API", type="primary", use_container_width=True):
            exito_c, msg_c = crear_tercero_express_siigo(
                nit=nit_ui, nombre=nombre_ui, apellidos=apellidos_ui, es_empresa="Empresa" in tipo_persona_ui,
                id_type=id_type_ui.split(" - ")[0].strip(), email=correo_ui, telefono=telefono_ui, direccion=direccion_ui,
                ciudad_dane=ciudad_ui.split(" - ")[0].strip(), resp_fiscal=resp_fiscal_ui.split(" - ")[0].strip(), tipo_tercero_str=tipo_tercero_ui
            )
            if exito_c: st.success(msg_c); st.rerun()
            else: st.error(msg_c)

@st.cache_data(ttl=1800)
def cargar_maestros_siigo(user, key):
    maestros = {"doc_types_fc": [], "doc_types_ds": [], "impuestos_iva": [{"id": 0, "nombre": "Ninguno (0%)", "porcentaje": 0}], "impuestos_rete": [{"id": 0, "nombre": "Ninguno (0%)", "porcentaje": 0}], "impuestos_ica": [{"id": 0, "nombre": "Ninguno (0%)", "porcentaje": 0}], "impuestos_reteiva": [{"id": 0, "nombre": "Ninguno (0%)", "porcentaje": 0}], "pagos": [], "centros_costo": [], "terceros": {}, "terceros_lista": [], "productos": [], "error": err}
    headers, err = get_siigo_headers()
    if not headers: return maestros

    try:
        for dt in requests.get("https://api.siigo.com/v1/document-types?type=FC", headers=headers).json(): maestros["doc_types_fc"].append({"id": dt["id"], "nombre": f"FC - {dt['code']} - {dt['name']}"})
        for dt in requests.get("https://api.siigo.com/v1/document-types?type=DS", headers=headers).json(): maestros["doc_types_ds"].append({"id": dt["id"], "nombre": f"DS - {dt['code']} - {dt['name']}"})
        for i in requests.get("https://api.siigo.com/v1/taxes", headers=headers).json():
            if i.get("active", True):
                item = {"id": i["id"], "nombre": f"{i['name']} {i['percentage']}%", "porcentaje": float(i.get("percentage", 0))}
                tipo, nombre = (i.get("type") or "").upper(), (i.get("name") or "").upper()
                if "RETEIVA" in tipo or "RETEIVA" in nombre: maestros["impuestos_reteiva"].append(item)
                elif "IVA" in tipo and "RETE" not in tipo: maestros["impuestos_iva"].append(item)
                elif "ICA" in tipo: maestros["impuestos_ica"].append(item)
                else: maestros["impuestos_rete"].append(item)
        for p in requests.get("https://api.siigo.com/v1/payment-types?document_type=FC", headers=headers).json(): maestros["pagos"].append({"id": p["id"], "nombre": p['name']})
        for cc in requests.get("https://api.siigo.com/v1/cost-centers", headers=headers).json(): maestros["centros_costo"].append({"id": cc["id"], "nombre": f"{cc['code']} - {cc['name']}"})
        page = 1
        while True:
            res_terc = requests.get(f"https://api.siigo.com/v1/customers?page={page}&page_size=100", headers=headers)
            if res_terc.status_code != 200 or not res_terc.json().get("results"): break
            for t in res_terc.json().get("results", []):
                nit = str(t.get("identification", "")).strip()
                nombre = t.get("name", [""])[0] if isinstance(t.get("name"), list) else t.get("person_name", {}).get("first_name", "Proveedor")
                if nit: maestros["terceros"][nit] = f"{nit} - {nombre}"; maestros["terceros_lista"].append(f"{nit} - {nombre}")
            page += 1
        page = 1
        while True:
            res_prod = requests.get(f"https://api.siigo.com/v1/products?page={page}&page_size=100", headers=headers)
            if res_prod.status_code != 200 or not res_prod.json().get("results"): break
            for pr in res_prod.json().get("results", []): maestros["productos"].append(f"{pr.get('code')} - {pr.get('name')}")
            page += 1
    except Exception as e: maestros["error"] = str(e)
    return maestros

def procesar_excel_puc(file_obj):
    try:
        df = pd.read_excel(file_obj) if file_obj.name.endswith(('.xlsx', '.xls')) else pd.read_csv(file_obj)
        cols = list(df.columns)
        code_col, name_col, level_col, status_col = cols[0] if cols else None, cols[1] if len(cols)>1 else None, None, None
        for c in cols:
            c_str = str(c).lower().strip()
            if any(k in c_str for k in ["cód", "cod", "cuenta", "numero"]): code_col = c
            if any(k in c_str for k in ["nom", "desc", "concepto", "denominaci"]): name_col = c
            if any(k in c_str for k in ["nivel", "agrupaci", "tipo_cuenta", "clase"]): level_col = c
            if any(k in c_str for k in ["estad", "activ", "state"]): status_col = c

        puc_list = []
        if code_col and name_col:
            for _, row in df.iterrows():
                val_code, val_name = str(row[code_col]).strip(), str(row[name_col]).strip()
                code_digits = re.sub(r'[^\d]', '', val_code)
                es_transaccional, es_activa = True, True
                if level_col and pd.notnull(row[level_col]):
                    lev_str = str(row[level_col]).lower().strip()
                    if any(k in lev_str for k in ["agrup", "mayor", "titulo", "subcuenta", "no", "falso", "false", "0"]) and "transaccional" not in lev_str and "auxiliar" not in lev_str: es_transaccional = False
                if status_col and pd.notnull(row[status_col]) and str(row[status_col]).lower().strip() in ["inactiva", "inactivo", "bloqueada", "no", "falso", "false", "0"]: es_activa = False
                if code_digits and val_name.lower() != "nan" and es_transaccional and es_activa: puc_list.append(f"{code_digits} - {val_name}")
        return list(dict.fromkeys(puc_list)) if puc_list else None
    except Exception: return None

def buscar_indice_tercero(nombre_prov, nit_prov, terceros_lista):
    if not terceros_lista: return -1
    nit_clean = re.sub(r'\D', '', str(nit_prov or ''))
    if nit_clean:
        for idx, item in enumerate(terceros_lista):
            if re.sub(r'\D', '', item.split(" - ")[0].strip()) == nit_clean: return idx
    p_clean = (nombre_prov or "").lower().strip()
    if not p_clean: return -1
    best_idx, best_score = -1, 0.65
    for idx, item in enumerate(terceros_lista):
        score = difflib.SequenceMatcher(None, p_clean, item.lower()).ratio()
        if score > best_score: best_score, best_idx = score, idx
    return best_idx

def buscar_indice_iva(iva_pct_xml, list_iva):
    if not iva_pct_xml or float(iva_pct_xml) == 0: return 0
    for idx, imp in enumerate(list_iva):
        if abs(float(imp.get("porcentaje", 0)) - float(iva_pct_xml)) < 0.5: return idx
    return 0

class PredictiveEngine:
    def __init__(self, masters: Dict[str, Any], history: List[dict] = None): self.masters = masters
    def predict_mapping(self, provider_name: str, nit: str, descripcion: str) -> dict:
        clean_name = (provider_name or "").lower().strip()
        cc_def = self.masters.get("centros_costo", [{}])[0].get("id") if self.masters.get("centros_costo") else None
        if any(k in clean_name for k in ["google", "docusign", "monday"]): return {"puc_code": "51355001", "cost_center_id": cc_def}
        elif any(k in clean_name for k in ["bernal", "william", "factotal", "ft capital"]): return {"puc_code": "51353501", "cost_center_id": cc_def}
        return {"puc_code": "51353501", "cost_center_id": cc_def}

@st.cache_data(ttl=3600)
def consultar_trm_oficial_script(fecha_str):
    try:
        url = f"https://www.datos.gov.co/resource/32sa-8pi3.json?$where=vigenciadesde<='{fecha_str[:10]}T23:59:59.000'&$order=vigenciadesde DESC&$limit=1"
        res = requests.get(url, timeout=5)
        if res.status_code == 200 and len(res.json()) > 0: return float(res.json()[0]['valor'])
    except Exception: pass
    return 3995.00

def causar_en_siigo_api(payload, is_ds=False):
    headers, err = get_siigo_headers()
    if not headers: return False, f"No Token: {err}", None, None
    url_envio = "https://api.siigo.com/v1/purchase-support-documents" if is_ds else "https://api.siigo.com/v1/purchases"
    try:
        res = requests.post(url_envio, json=payload, headers=headers, timeout=10)
        res_json = res.json()
        if res.status_code == 400 and res_json.get("errors") and res_json["errors"][0].get("code") == "invalid_total_payments":
            match = re.search(r'calculated is (\d+(\.\d+)?)', res_json["errors"][0].get("message", ""))
            if match:
                payload["payments"][0]["value"] = round(float(match.group(1)), 2)
                res = requests.post(url_envio, json=payload, headers=headers, timeout=10)
                res_json = res.json()

        if res.status_code in [200, 201]: return True, f"✅ EXITOSO en Siigo", res_json.get("id"), res_json.get('name') or f"{'DS' if is_ds else 'Compra'} No. {res_json.get('number', '')}"
        else: return False, f"❌ ERROR: {res.text}", None, None
    except Exception as e: return False, f"❌ Error de Red: {e}", None, None

def obtener_pdf_siigo_api(doc_id, is_ds=False):
    headers, err = get_siigo_headers()
    if not headers: return None
    try:
        res = requests.get(f"https://api.siigo.com/v1/purchase-support-documents/{doc_id}/pdf" if is_ds else f"https://api.siigo.com/v1/purchases/{doc_id}/pdf", headers=headers, timeout=10)
        if res.status_code == 200:
            data = res.json()
            if "base64" in data: return base64.b64decode(data["base64"])
            elif "pdf" in data: return base64.b64decode(data["pdf"])
            elif "url" in data:
                res_pdf = requests.get(data["url"], timeout=10)
                if res_pdf.status_code == 200: return res_pdf.content
    except Exception: pass
    return None

def parse_ubl_xml(xml_content, pdf_bytes_adjunto=None):
    try:
        root = ET.fromstring(xml_content)
        for elem in root.iter():
            if '}' in elem.tag: elem.tag = elem.tag.split('}', 1)[1]
        if root.tag == "AttachedDocument":
            attachment = root.find(".//Attachment/ExternalReference/Description")
            if attachment is not None and attachment.text:
                try:
                    root = ET.fromstring(attachment.text)
                    for elem in root.iter():
                        if '}' in elem.tag: elem.tag = elem.tag.split('}', 1)[1]
                except Exception: pass

        factura_id_raw = root.findtext(".//ID") or "1"
        factura_id_solo_num = re.sub(r'\D', '', factura_id_raw) or factura_id_raw
        fecha = root.findtext(".//IssueDate") or datetime.now().strftime("%Y-%m-%d")
        supplier_node = root.find(".//AccountingSupplierParty")
        supplier_name = supplier_node.findtext(".//RegistrationName") or supplier_node.findtext(".//Name") or "" if supplier_node is not None else ""
        supplier_nit = supplier_node.findtext(".//CompanyID") or "" if supplier_node is not None else ""

        pdf_bytes = pdf_bytes_adjunto
        if not pdf_bytes:
            for b64_node in root.findall(".//EmbeddedDocumentBinaryObject"):
                if b64_node.text:
                    try: pdf_bytes = base64.b64decode(b64_node.text); break
                    except Exception: pass

        lineas_detalle = []
        subtotal_factura, iva_factura = 0.0, 0.0
        for linea in root.findall(".//InvoiceLine") or root.findall(".//CreditNoteLine"):
            desc_node = linea.find(".//Item/Description") or linea.find(".//Description")
            concepto = desc_node.text if desc_node is not None else "Sin descripción"
            qty = float(linea.findtext(".//InvoicedQuantity") or linea.findtext(".//CreditedQuantity") or 1.0)
            precio_uni = float(linea.findtext(".//Price/PriceAmount") or 0.0)
            subtotal_linea = float(linea.findtext(".//LineExtensionAmount") or (qty * precio_uni))

            iva_pct, iva_valor = 0.0, 0.0
            for tax in linea.findall(".//TaxTotal/TaxSubtotal"):
                tax_name = tax.findtext(".//TaxScheme/Name") or ""
                if tax.findtext(".//TaxScheme/ID") == "01" or "IVA" in tax_name.upper():
                    iva_pct, iva_valor = float(tax.findtext(".//Percent") or 0), float(tax.findtext(".//TaxAmount") or 0)

            lineas_detalle.append({"Concepto": concepto, "Cantidad": qty, "Valor Unitario": precio_uni, "Subtotal": subtotal_linea, "IVA %": iva_pct, "Valor IVA": iva_valor, "Total Línea": subtotal_linea + iva_valor})
            subtotal_factura += subtotal_linea; iva_factura += iva_valor

        monetary_node = root.find(".//LegalMonetaryTotal")
        total_oficial = float(monetary_node.findtext(".//PayableAmount") or (subtotal_factura + iva_factura)) if monetary_node is not None else (subtotal_factura + iva_factura)

        return {"tipo_origen": "FC", "Resumen": {"Tipo": "Factura", "ID": factura_id_solo_num, "Fecha": fecha, "NIT": supplier_nit, "Proveedor": supplier_name, "Subtotal": subtotal_factura, "IVA": iva_factura, "Total": total_oficial, "Estado": "Pendiente", "Moneda": "COP", "CentroCosto": None}, "Detalle": lineas_detalle, "pdf_b64": base64.b64encode(pdf_bytes).decode('utf-8') if pdf_bytes else None}
    except Exception: return None

def process_bytes(file_name, file_bytes, data_list):
    if file_name.lower().endswith(".zip"):
        try:
            with zipfile.ZipFile(io.BytesIO(file_bytes)) as z:
                pdf_map = {os.path.splitext(f)[0]: z.read(f) for f in z.namelist() if f.lower().endswith(".pdf")}
                for f in z.namelist():
                    if f.lower().endswith(".xml"):
                        parsed = parse_ubl_xml(z.read(f), pdf_map.get(os.path.splitext(f)[0]) or (list(pdf_map.values())[0] if pdf_map else None))
                        if parsed: data_list.append(parsed)
        except Exception: pass
    elif file_name.lower().endswith(".xml"):
        parsed = parse_ubl_xml(file_bytes)
        if parsed: data_list.append(parsed)

def extraer_datos_pdf_soporte(pdf_bytes, filename):
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        texto_upper = "".join([page.extract_text() or "" for page in reader.pages]).upper()
    except Exception: texto_upper = ""

    proveedor, nit_prov = "Proveedor Desconocido", "900123456"
    if "BERNAL" in texto_upper or "WILLIAM" in texto_upper: proveedor, nit_prov = "William Fernando Bernal Gacha", "80799567"
    elif "GOOGLE" in texto_upper: proveedor, nit_prov = "Google LLC", "901300354"
    elif "DOCUSIGN" in texto_upper: proveedor, nit_prov = "DocuSign, Inc.", "900987654"
    elif "MONDAY" in texto_upper: proveedor, nit_prov = "Monday.com LTD", "514744887"
    elif "FACTOTAL" in texto_upper: proveedor, nit_prov = "FACTOTAL COLOMBIA S.A.S.", "901405289"

    m_inv = re.search(r'(?:NÚMERO DE FACTURA|INVOICE #|CUENTA DE COBRO N°|FACTURA N°|INVOICE NUMBER|INVOICE NO)[:\s]*([A-Z0-9\-]+)', texto_upper)
    id_doc_solo_num = re.sub(r'\D', '', m_inv.group(1) if m_inv else (re.search(r'#\s*([A-Z0-9\-]+)', texto_upper).group(1) if re.search(r'#\s*([A-Z0-9\-]+)', texto_upper) else "101")) or "101"

    moneda = "USD" if any(k in texto_upper for k in ["USD", "DOLLARS", "GOOGLE", "DOCUSIGN", "MONDAY"]) and not ("COP" in texto_upper and "GOOGLE" not in texto_upper) else "COP"

    total_monto = 0.0
    for pat in ([r'(?:IMPORTE TOTAL ADEUDADO EN USD|TOTAL IN USD|TOTAL PRICE IN USD|TOTAL|SUBTOTAL)\s*(?:USD|\$)?\s*([0-9,]+\.[0-9]{2})', r'USD\s*[\$]?\s*([0-9,]+\.[0-9]{2})', r'([0-9,]+\.[0-9]{2})'] if moneda == "USD" else [r'SUMA DE[:\s]*.*?([0-9\.]+)', r'\$\s*([0-9\.]+)', r'([0-9\.]+)\s*COP']):
        matches = re.findall(pat, texto_upper)
        if matches:
            vals = [float(m.replace(',', '')) for m in matches if float(m.replace(',', '')) > 0] if moneda == "USD" else [float(m.replace('.', '')) for m in matches if m.replace('.', '').isdigit() and float(m.replace('.', '')) > 1000]
            if vals: total_monto = max(vals); break

    fecha_emision = datetime.now().strftime("%Y-%m-%d")
    m_date_es = re.search(r'(\d{1,2})\s+(?:de\s+)?([a-zA-Z]{3,10})\s+(?:de\s+)?(\d{4})', texto_upper.lower())
    if m_date_es:
        months_es = {"ene": "01", "feb": "02", "mar": "03", "abr": "04", "may": "05", "jun": "06", "jul": "07", "ago": "08", "sep": "09", "oct": "10", "nov": "11", "dic": "12", "enero": "01", "febrero": "02", "marzo": "03", "abril": "04", "mayo": "05", "junio": "06", "julio": "07", "agosto": "08", "septiembre": "09", "octubre": "10", "noviembre": "11", "diciembre": "12"}
        months_en = {"jan": "01", "feb": "02", "mar": "03", "apr": "04", "may": "05", "jun": "06", "jul": "07", "aug": "08", "sep": "09", "oct": "10", "nov": "11", "dec": "12"}
        month = months_es.get(m_date_es.group(2)[:3].lower()) or months_en.get(m_date_es.group(2)[:3].lower())
        if month: fecha_emision = f"{m_date_es.group(3)}-{month}-{m_date_es.group(1).zfill(2)}"

    return {"tipo_origen": "DS", "archivo": filename, "pdf_b64": base64.b64encode(pdf_bytes).decode('utf-8') if pdf_bytes else None, "proveedor": proveedor, "nit": nit_prov, "documento_ref": id_doc_solo_num, "fecha": fecha_emision, "moneda_origen": moneda, "monto_origen": total_monto, "trm": consultar_trm_oficial_script(fecha_emision) if moneda == "USD" else 1.0, "estado": "Pendiente", "centro_costo": None, "causado": False, "items_custom": []}

# ==========================================
# BARRA SUPERIOR Y MÁSTERES
# ==========================================
c1, c2, c3, c4 = st.columns([1.8, 2.2, 1.5, 1.5])
with c1:
    if st.button("🔄 Sincronizar Maestros Siigo", use_container_width=True):
        st.cache_data.clear(); maestros = cargar_maestros_siigo(curr_tenant['siigo_user'], curr_tenant['siigo_key'])
        st.success("¡Maestros actualizados!")
    else: maestros = cargar_maestros_siigo(curr_tenant['siigo_user'], curr_tenant['siigo_key'])

with c2:
    if can_admin:
        excel_puc = st.file_uploader("📥 Cargar PUC (Excel)", type=["xlsx", "xls", "csv"], label_visibility="collapsed")
        if excel_puc:
            file_key = f"{curr_tenant_nit}_{excel_puc.name}_{excel_puc.size}"
            if st.session_state.get('last_puc_key') != file_key:
                nuevos_puc = procesar_excel_puc(excel_puc)
                if nuevos_puc:
                    conn = get_db_connection()
                    conn.execute("UPDATE tenants SET puc=? WHERE nit=?", (json.dumps(nuevos_puc), curr_tenant_nit))
                    conn.commit(); conn.close()
                    st.session_state['last_puc_key'] = file_key
                    curr_tenant['puc'] = nuevos_puc
                    st.toast(f"✅ Se cargaron {len(nuevos_puc)} cuentas ACTIVAS Y TRANSACCIONALES del PUC.", icon="📚")

with c3: st.caption(f"📊 **Maestros:** Terceros `{len(maestros.get('terceros', {}))}` | PUC `{len(curr_tenant.get('puc', []))}`")
with c4:
    if st.button("🚪 Cerrar Sesión", use_container_width=True):
        st.session_state['authenticated_user'] = None; st.rerun()

# NAVEGACIÓN MODULAR
tab1, tab2, tab3, tab4, tab_rep, tab_tenant = st.tabs([
    "📥 1. Recepción & Aprobación", "🧠 2. Causación FC", "📄 3. Documento Soporte DS", 
    "📊 4. Tablero de Audit", "📈 5. Reportes", "⚙️ Configuración Empresa"
])

# ==========================================
# PESTAÑA 1: RECEPCIÓN, APROBACIÓN Y RECHAZOS
# ==========================================
with tab1:
    st.subheader("📥 Recepción, Aprobación y Rechazo de Documentos")
    st.caption("Gestión de Aprobaciones / Rechazos para la Empresa Activa.")
    terceros_dict = maestros.get("terceros", {})
    cc_lista = maestros.get("centros_costo", [])
    cc_opciones = ["-- Sin Centro de Costo (Opcional) --"] + [c["nombre"] for c in cc_lista]

    subtab_fc, subtab_ds = st.tabs(["🧾 Facturas de Compra (FC)", "📄 Documentos Soporte (DS)"])

    with subtab_fc:
        if can_upload:
            st.markdown("##### 📤 Subir Facturas de Compra (XML / ZIP)")
            uploaded_fc = st.file_uploader("Adjunta archivos XML o ZIP", type=["zip", "xml"], accept_multiple_files=True, key="up_fc_p1")
            if uploaded_fc:
                if st.button("🚀 Procesar Facturas Subidas", key="btn_proc_fc", type="primary", use_container_width=True):
                    data_list = []
                    for file in uploaded_fc: process_bytes(file.name, file.read(), data_list)
                    proc_count, skipped_list = 0, []
                    for item in data_list:
                        doc_ref = item["Resumen"]["ID"]
                        is_proc, razon = db_is_doc_already_processed(curr_tenant_nit, doc_ref, "FC")
                        if not is_proc:
                            db_save_doc(curr_tenant_nit, doc_ref, "FC", "Pendiente", item); proc_count += 1
                        else: skipped_list.append(f"Factura FC-{doc_ref} ({item['Resumen']['Proveedor']}): {razon}")
                    st.session_state['result_upload_fc'] = {"added": proc_count, "skipped": skipped_list}; st.rerun()

            if 'result_upload_fc' in st.session_state:
                res = st.session_state.pop('result_upload_fc')
                if res["added"] > 0: st.success(f"✅ Se cargaron {res['added']} nuevas Facturas de Compra.")
                if res["skipped"]: st.warning("⚠️ **Documentos NO procesados (Ya existen):**\n" + "\n".join([f"* {item}" for item in res["skipped"]]))

        st.markdown("---")
        fc_sub_tab1, fc_sub_tab2 = st.tabs(["⏳ FC Pendientes", "🚫 FC Rechazadas"])

        with fc_sub_tab1:
            fc_pendientes = db_get_docs(curr_tenant_nit, "FC", "Pendiente")
            if not fc_pendientes: st.info("No hay Facturas de Compra pendientes de aprobación.")
            else:
                for idx, f in enumerate(fc_pendientes):
                    r = f["Resumen"]
                    clean_nit = re.sub(r'\D', '', str(r["NIT"]))
                    esta_en_siigo = clean_nit in terceros_dict
                    with st.container(border=True):
                        c1, c2, c3, c4, c5, c6, c7, c8 = st.columns([1, 2.2, 1, 1.1, 0.7, 2.2, 1.2, 1.5])
                        with c1: st.markdown(f"**FC-{r['ID']}**")
                        with c2: st.markdown(f"**{r['Proveedor']}**"); st.caption(f"NIT: {clean_nit}")
                        with c3: st.markdown(f"{r['Fecha']}")
                        with c4: st.markdown(f"**${r['Total']:,.2f}**")
                        with c5: st.markdown("**COP**")
                        with c6:
                            if can_approve:
                                sel_cc = st.selectbox("CC", options=cc_opciones, index=cc_opciones.index(r.get("CentroCosto")) if r.get("CentroCosto") in cc_opciones else 0, key=f"fc_p1_cc_{idx}", label_visibility="collapsed")
                                r["CentroCosto"] = None if sel_cc == "-- Sin Centro de Costo (Opcional) --" else sel_cc
                        with c7:
                            if esta_en_siigo: st.markdown("<span class='badge-ok'>✅ Registrado</span>", unsafe_allow_html=True)
                            else:
                                st.markdown("<span class='badge-warn'>🔴 No Creado</span>", unsafe_allow_html=True)
                                if can_approve and st.button("➕ Crear en Siigo", key=f"btn_create_terc_fc_{idx}"): modal_formulario_tercero(clean_nit, r['Proveedor'], es_extranjero=False)
                        with c8:
                            if can_approve:
                                btn_ap, btn_rec = st.columns(2)
                                with btn_ap:
                                    if st.button("✅ Aprobar", key=f"btn_ap_fc_{idx}"):
                                        r["Estado"] = "Aprobado"
                                        r["UsuarioAprobador"] = curr_user["email"]
                                        r["FechaAprobacion"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                                        db_save_doc(curr_tenant_nit, r['ID'], "FC", "Aprobado", f)
                                        st.toast(f"✅ FC-{r['ID']} Aprobada.", icon="🎉"); st.rerun()
                                with btn_rec:
                                    if st.button("❌ Rechazar", key=f"btn_rec_fc_{idx}"):
                                        r["Estado"] = "Rechazado"
                                        r["UsuarioAprobador"] = curr_user["email"]
                                        r["FechaAprobacion"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                                        db_save_doc(curr_tenant_nit, r['ID'], "FC", "Rechazado", f)
                                        st.toast(f"🚫 FC-{r['ID']} Rechazada.", icon="🗑️"); st.rerun()
                        with st.expander(f"👁️ Detalle e Ítems de FC-{r['ID']}", expanded=False):
                            d_col1, d_col2 = st.columns([3, 1])
                            with d_col1: st.dataframe(pd.DataFrame(f["Detalle"]), use_container_width=True)
                            with d_col2:
                                if f.get("pdf_b64"): st.download_button("💾 PDF Original", data=base64.b64decode(f["pdf_b64"]), file_name=f"Original_FC_{r['ID']}.pdf", mime="application/pdf", key=f"dl_fc_pdf_{idx}")

        with fc_sub_tab2:
            fc_rechazadas = db_get_docs(curr_tenant_nit, "FC", "Rechazado")
            if not fc_rechazadas: st.info("No hay Facturas de Compra rechazadas.")
            else:
                for idx, f in enumerate(fc_rechazadas):
                    r = f["Resumen"]
                    with st.container(border=True):
                        c1, c2, c3, c4, c5, c6 = st.columns([1.5, 3, 1.5, 1.5, 2, 2])
                        with c1: st.markdown(f"**FC-{r['ID']}**")
                        with c2: st.markdown(f"**{r['Proveedor']}**"); st.caption(f"NIT: {r['NIT']}")
                        with c3: st.markdown(f"{r['Fecha']}")
                        with c4: st.markdown(f"**${r['Total']:,.2f}**")
                        with c5: st.markdown("<span class='badge-danger'>🚫 RECHAZADA</span>", unsafe_allow_html=True)
                        with c6:
                            b_act1, b_act2 = st.columns(2)
                            with b_act1:
                                if st.button("🔄 Re-evaluar", key=f"btn_reopen_fc_{idx}"):
                                    r["Estado"] = "Pendiente"; db_save_doc(curr_tenant_nit, r['ID'], "FC", "Pendiente", f); st.rerun()
                            with b_act2:
                                if st.button("🗑️ Eliminar", key=f"btn_del_perm_fc_{idx}"): db_delete_doc(curr_tenant_nit, r['ID'], "FC"); st.rerun()

    with subtab_ds:
        if can_upload:
            st.markdown("##### 📤 Subir Documentos Soporte (PDF)")
            uploaded_ds = st.file_uploader("Adjunta archivos PDF", type=["pdf"], accept_multiple_files=True, key="up_ds_p1")
            if uploaded_ds:
                if st.button("🚀 Procesar Documentos Soporte Subidos", key="btn_proc_ds", type="primary", use_container_width=True):
                    nuevos_ds = [extraer_datos_pdf_soporte(f.read(), f.name) for f in uploaded_ds]
                    proc_count, skipped_list = 0, []
                    for item in nuevos_ds:
                        if item:
                            doc_ref = item["documento_ref"]
                            is_proc, razon = db_is_doc_already_processed(curr_tenant_nit, doc_ref, "DS")
                            if not is_proc: db_save_doc(curr_tenant_nit, doc_ref, "DS", "Pendiente", item); proc_count += 1
                            else: skipped_list.append(f"Doc Soporte DS-{doc_ref} ({item['proveedor']}): {razon}")
                    st.session_state['result_upload_ds'] = {"added": proc_count, "skipped": skipped_list}; st.rerun()

            if 'result_upload_ds' in st.session_state:
                res = st.session_state.pop('result_upload_ds')
                if res["added"] > 0: st.success(f"✅ Se cargaron {res['added']} nuevos Documentos Soporte.")
                if res["skipped"]: st.warning("⚠️ **Documentos NO procesados (Ya existen):**\n" + "\n".join([f"* {item}" for item in res["skipped"]]))

        st.markdown("---")
        ds_sub_tab1, ds_sub_tab2 = st.tabs(["⏳ DS Pendientes", "🚫 DS Rechazados"])

        with ds_sub_tab1:
            ds_pendientes = db_get_docs(curr_tenant_nit, "DS", "Pendiente")
            if not ds_pendientes: st.info("No hay Documentos Soporte pendientes de aprobación.")
            else:
                for idx, d in enumerate(ds_pendientes):
                    clean_nit = re.sub(r'\D', '', str(d["nit"]))
                    esta_en_siigo = clean_nit in terceros_dict
                    with st.container(border=True):
                        c1, c2, c3, c4, c5, c6, c7, c8 = st.columns([1, 2.2, 1, 1.1, 0.7, 2.2, 1.2, 1.5])
                        with c1: st.markdown(f"**DS-{d['documento_ref']}**")
                        with c2: st.markdown(f"**{d['proveedor']}**"); st.caption(f"NIT: {clean_nit}")
                        with c3: st.markdown(f"{d['fecha']}")
                        with c4: st.markdown(f"**{'$' if d['moneda_origen'] == 'COP' else 'USD $'}{d['monto_origen']:,.2f}**")
                        with c5: st.markdown(f"**{d['moneda_origen']}**")
                        with c6:
                            if can_approve:
                                sel_cc = st.selectbox("CC", options=cc_opciones, index=cc_opciones.index(d.get("centro_costo")) if d.get("centro_costo") in cc_opciones else 0, key=f"ds_p1_cc_{idx}", label_visibility="collapsed")
                                d["centro_costo"] = None if sel_cc == "-- Sin Centro de Costo (Opcional) --" else sel_cc
                        with c7:
                            if esta_en_siigo: st.markdown("<span class='badge-ok'>✅ Registrado</span>", unsafe_allow_html=True)
                            else:
                                st.markdown("<span class='badge-warn'>🔴 No Creado</span>", unsafe_allow_html=True)
                                if can_approve and st.button("➕ Crear en Siigo", key=f"btn_create_terc_ds_{idx}"): modal_formulario_tercero(clean_nit, d['proveedor'], es_extranjero=(d['moneda_origen'] == "USD"))
                        with c8:
                            if can_approve:
                                btn_ap, btn_rec = st.columns(2)
                                with btn_ap:
                                    if st.button("✅ Aprobar", key=f"btn_ap_ds_{idx}"):
                                        d["estado"] = "Aprobado"
                                        d["UsuarioAprobador"] = curr_user["email"]
                                        d["FechaAprobacion"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                                        db_save_doc(curr_tenant_nit, d['documento_ref'], "DS", "Aprobado", d)
                                        st.toast(f"✅ DS-{d['documento_ref']} Aprobado.", icon="🎉"); st.rerun()
                                with btn_rec:
                                    if st.button("❌ Rechazar", key=f"btn_rec_ds_{idx}"):
                                        d["estado"] = "Rechazado"
                                        d["UsuarioAprobador"] = curr_user["email"]
                                        d["FechaAprobacion"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                                        db_save_doc(curr_tenant_nit, d['documento_ref'], "DS", "Rechazado", d)
                                        st.toast(f"🚫 DS-{d['documento_ref']} Rechazado.", icon="🗑️"); st.rerun()

        with ds_sub_tab2:
            ds_rechazados = db_get_docs(curr_tenant_nit, "DS", "Rechazado")
            if not ds_rechazados: st.info("No hay Documentos Soporte rechazados.")
            else:
                for idx, d in enumerate(ds_rechazados):
                    with st.container(border=True):
                        c1, c2, c3, c4, c5, c6 = st.columns([1.5, 3, 1.5, 1.5, 2, 2])
                        with c1: st.markdown(f"**DS-{d['documento_ref']}**")
                        with c2: st.markdown(f"**{d['proveedor']}**"); st.caption(f"NIT: {d['nit']}")
                        with c3: st.markdown(f"{d['fecha']}")
                        with c4: st.markdown(f"**{'$' if d['moneda_origen'] == 'COP' else 'USD $'}{d['monto_origen']:,.2f}**")
                        with c5: st.markdown("<span class='badge-danger'>🚫 RECHAZADO</span>", unsafe_allow_html=True)
                        with c6:
                            b_act1, b_act2 = st.columns(2)
                            with b_act1:
                                if st.button("🔄 Re-evaluar", key=f"btn_reopen_ds_{idx}"):
                                    d["estado"] = "Pendiente"; db_save_doc(curr_tenant_nit, d['documento_ref'], "DS", "Pendiente", d); st.rerun()
                            with b_act2:
                                if st.button("🗑️ Eliminar", key=f"btn_del_perm_ds_{idx}"): db_delete_doc(curr_tenant_nit, d['documento_ref'], "DS"); st.rerun()

# ==========================================
# PESTAÑA 2: CAUSACIÓN FACTURA DE COMPRA (FC)
# ==========================================
with tab2:
    if not can_cause: st.warning("🔒 No tienes permisos para causar en Siigo.")
    else:
        st.subheader("🧠 Pre-causación Factura de Compra (FC)")
        fc_aprobadas = db_get_docs(curr_tenant_nit, "FC", "Aprobado")
        if not fc_aprobadas: st.info("🎉 No hay facturas pendientes por causar.")
        else:
            terceros_lista = maestros.get("terceros_lista", [])
            types_fc = maestros.get("doc_types_fc", [{"id": 19147, "nombre": "FC - 1 - Compra (ID: 19147)"}])
            pagos_lista = maestros.get("pagos", [{"id": 1, "nombre": "Efectivo / Crédito (ID: 1)"}])
            prods_lista = maestros.get("productos", [])
            list_iva, list_rete, list_reteiva, list_ica = maestros.get("impuestos_iva", []), maestros.get("impuestos_rete", []), maestros.get("impuestos_reteiva", []), maestros.get("impuestos_ica", [])
            engine = PredictiveEngine(maestros)

            for idx, doc in enumerate(fc_aprobadas):
                r = doc["Resumen"]
                llave_factura = f"{r['NIT']}_{r['ID']}"
                with st.container(border=True):
                    c_head1, c_head2 = st.columns([4, 1])
                    with c_head1:
                        with st.expander(f"👁️ Ver Detalle de Factura {r['ID']} ({r['Proveedor']})", expanded=False): st.dataframe(pd.DataFrame(doc["Detalle"]), use_container_width=True)
                    with c_head2:
                        if doc.get("pdf_b64"): st.download_button("💾 PDF Factura", data=base64.b64decode(doc["pdf_b64"]), file_name=f"Factura_{r['ID']}.pdf", mime="application/pdf", key=f"dl_fac_pdf_{idx}")
                    
                    e1, e2, e3, e4 = st.columns([2, 1.8, 1.8, 1.5])
                    with e1:
                        dt_sel = st.selectbox("Tipo", options=[t["nombre"] for t in types_fc], key=f"fc_dt_{idx}")
                        id_type_fc = next((t["id"] for t in types_fc if t["nombre"] == dt_sel), 19147)
                        idx_terc_def = buscar_indice_tercero(r["Proveedor"], r["NIT"], terceros_lista)
                        opciones_terc = terceros_lista.copy()
                        if idx_terc_def < 0:
                            opciones_terc.insert(0, f"⚠️ TERCERO NO CREADO EN SIIGO ({r['NIT']} - {r['Proveedor']})")
                            tercero_sel = st.selectbox("Proveedores", options=opciones_terc, index=0, key=f"fc_terc_{idx}")
                        else: tercero_sel = st.selectbox("Proveedores", options=opciones_terc, index=idx_terc_def, key=f"fc_terc_{idx}")
                        nit_ingresado = tercero_sel.split(" - ")[0].strip() if "⚠️" not in tercero_sel else r["NIT"]
                    with e2:
                        fecha_fac = st.text_input("Fecha", value=r["Fecha"], key=f"fc_fec_{idx}")
                        num_fac = st.text_input("No. Factura", value=re.sub(r'\D', '', str(r['ID'])), key=f"fc_num_{idx}")
                    with e3:
                        cc_opts = ["-- Sin Centro de Costo --"] + [c["nombre"] for c in cc_lista]
                        cc_header_sel = st.selectbox("Centro de costo", options=cc_opts, index=cc_opts.index(r.get("CentroCosto")) if r.get("CentroCosto") in cc_opts else 0, key=f"fc_cc_head_{idx}")
                        id_cc_head = next((c["id"] for c in cc_lista if c["nombre"] == cc_header_sel), None) if cc_header_sel != "-- Sin Centro de Costo --" else None
                    with e4: st.metric("Total Neto XML", f"${r['Total']:,.0f}")

                    st.markdown("<div class='siigo-table-header'># | Tipo | Código / Producto | Descripción | Cant | Vr. Unitario | Imp. Cargo (IVA) | Imp. Retención | Valor Total | Acciones</div>", unsafe_allow_html=True)
                    items_siigo, acum_subtotal, acum_iva = [], 0.0, 0.0
                    detalles_fc = doc.get("Detalle", [])
                    
                    for item_idx, item in enumerate(detalles_fc):
                        i0, i1, i2, i3, i4, i5, i6, i7, i8, i9 = st.columns([0.3, 0.8, 1.8, 1.7, 0.5, 1.0, 1.3, 1.7, 0.9, 0.4])
                        with i0: st.markdown(f"**{item_idx+1}**")
                        with i1: tipo_item = st.selectbox("Tipo", options=["Account", "Product"], index=0, key=f"fc_tp_{llave_factura}_{item_idx}", label_visibility="collapsed")
                        
                        # CORRECCIÓN 2: SELECTOR DINÁMICO CUENTA VS PRODUCTO EN FC
                        with i2:
                            if tipo_item == "Account":
                                cat_puc = curr_tenant.get('puc', DEFAULT_PUC)
                                puc_sel = st.selectbox("Código PUC", options=cat_puc, index=0, key=f"puc_sel_{llave_factura}_{item_idx}", label_visibility="collapsed")
                                code_item = re.sub(r'[^\d]', '', puc_sel.split(" - ")[0].strip())
                            else:
                                prod_sel = st.selectbox("Producto Siigo", options=prods_lista if prods_lista else ["Sin Productos"], key=f"fc_prod_{llave_factura}_{item_idx}", label_visibility="collapsed")
                                code_item = prod_sel.split(" - ")[0].strip()

                        with i3: desc_val = st.text_input("Descripción", value=item['Concepto'], key=f"fc_desc_{llave_factura}_{item_idx}", label_visibility="collapsed")
                        with i4: cant_val = st.number_input("Cant", value=float(item.get('Cantidad', 1.0)), key=f"fc_cant_{llave_factura}_{item_idx}", label_visibility="collapsed")
                        with i5: monto_val = st.number_input("Vr. Unitario", value=float(item['Subtotal']), key=f"fc_val_{llave_factura}_{item_idx}", label_visibility="collapsed")
                        with i6:
                            iva_sel = st.selectbox("Imp. Cargo", options=[i["nombre"] for i in list_iva], index=buscar_indice_iva(item.get("IVA %", 0), list_iva), key=f"fc_iva_sel_{llave_factura}_{item_idx}", label_visibility="collapsed")
                            id_iva = next((i["id"] for i in list_iva if i["nombre"] == iva_sel), 0)
                            pct_iva_sel = next((i["porcentaje"] for i in list_iva if i["nombre"] == iva_sel), 0.0)
                        with i7:
                            rete_sel = st.selectbox("Imp. Retención", options=[i["nombre"] for i in list_rete], key=f"fc_rete_sel_{llave_factura}_{item_idx}", label_visibility="collapsed")
                            id_rete = next((i["id"] for i in list_rete if i["nombre"] == rete_sel), 0)
                        with i8:
                            sub_row = round(monto_val * cant_val, 2); iva_row = round(sub_row * (pct_iva_sel / 100.0), 2)
                            st.markdown(f"**${sub_row + iva_row:,.0f}**")
                        with i9:
                            if len(detalles_fc) > 1 and st.button("🗑️", key=f"btn_del_line_fc_{llave_factura}_{item_idx}"):
                                doc["Detalle"].pop(item_idx); db_save_doc(curr_tenant_nit, r['ID'], "FC", "Aprobado", doc); st.rerun()
                        acum_subtotal += sub_row; acum_iva += iva_row
                        items_siigo.append({"code": code_item, "type": tipo_item, "description": desc_val, "quantity": cant_val, "price": monto_val, "cost_center": id_cc_head, "id_iva": id_iva, "id_rete": id_rete})

                    if st.button("➕ Agregar Línea Adicional", key=f"btn_add_line_fc_{idx}"):
                        doc["Detalle"].append({"Concepto": "Línea Adicional", "Cantidad": 1.0, "Subtotal": 0.0, "IVA %": 0.0, "Valor IVA": 0.0}); db_save_doc(curr_tenant_nit, r['ID'], "FC", "Aprobado", doc); st.rerun()

                    st.markdown("---")
                    b1, b2, b3 = st.columns([2, 1.8, 1.8])
                    with b1:
                        pago_sel = st.selectbox("Forma de pago", options=[p["nombre"] for p in pagos_lista], key=f"fc_pago_{idx}")
                        id_pago = next((p["id"] for p in pagos_lista if p["nombre"] == pago_sel), 1)
                    with b2:
                        sel_reteiva = st.selectbox("ReteIVA (Pie)", options=[i["nombre"] for i in list_reteiva], key=f"fc_glob_reteiva_{idx}")
                        id_reteiva = next((i["id"] for i in list_reteiva if i["nombre"] == sel_reteiva), 0)
                    with b3:
                        sel_reteica = st.selectbox("ReteICA (Pie)", options=[i["nombre"] for i in list_ica], key=f"fc_glob_reteica_{idx}")
                        id_reteica = next((i["id"] for i in list_ica if i["nombre"] == sel_reteica), 0)

                    total_neto_calculado = acum_subtotal + acum_iva
                    st.markdown(f"### **Total Neto: ${total_neto_calculado:,.2f} COP**")

                    if st.button(f"🚀 Guardar y Enviar Factura FC en Siigo", key=f"btn_fc_{idx}", type="primary"):
                        if "⚠️" in tercero_sel: st.error("🔴 Selecciona un tercero válido.")
                        else:
                            num_fac_clean = re.sub(r'\D', '', str(num_fac)) or "1"
                            valid_items = [it for it in items_siigo if it["price"] > 0 or len(items_siigo) == 1]

                            final_items_payload = []
                            for it in valid_items:
                                taxes_list = []
                                if it["id_iva"] and int(it["id_iva"]) > 0: taxes_list.append({"id": int(it["id_iva"])})
                                if it["id_rete"] and int(it["id_rete"]) > 0: taxes_list.append({"id": int(it["id_rete"])})
                                item_dict = {"code": it["code"], "type": it["type"], "description": it["description"], "quantity": it["quantity"], "price": it["price"], "taxes": taxes_list}
                                if it["cost_center"]: item_dict["cost_center"] = it["cost_center"]
                                final_items_payload.append(item_dict)

                            retentions_payload = []
                            if id_reteiva and int(id_reteiva) > 0: retentions_payload.append({"id": int(id_reteiva)})
                            if id_reteica and int(id_reteica) > 0: retentions_payload.append({"id": int(id_reteica)})

                            payload_fc = {
                                "document": {"id": id_type_fc}, "date": fecha_fac, "supplier": {"identification": nit_ingresado, "branch_office": 0},
                                "retentions": retentions_payload, "observations": f"Causación AutoCount.ai - Doc {num_fac_clean}",
                                "items": final_items_payload, "payments": [{"id": id_pago, "value": round(total_neto_calculado, 2), "due_date": fecha_fac}],
                                "provider_invoice": {"prefix": "FC", "number": int(str(num_fac_clean)[:9])}
                            }
                            if id_cc_head: payload_fc["cost_center"] = id_cc_head

                            exito, msg, doc_id_siigo, doc_num_siigo = causar_en_siigo_api(payload_fc, is_ds=False)
                            if exito:
                                siigo_ref_completa = f"{doc_num_siigo}|||{doc_id_siigo}"
                                doc["Resumen"]["Subtotal"] = acum_subtotal
                                doc["Resumen"]["IVA"] = acum_iva
                                doc["Resumen"]["Total"] = total_neto_calculado
                                doc["UsuarioCausador"] = curr_user["email"]
                                doc["FechaCausacion"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                                db_save_history(curr_tenant_nit, r['ID'], "FC", fecha_fac, total_neto_calculado, "COP", siigo_ref_completa, r["Proveedor"], nit_ingresado, doc.get("pdf_b64"), json.dumps(doc), curr_user["email"])
                                st.toast(f"✅ Causada exitosamente: {doc_num_siigo}", icon="🎉"); st.rerun()
                            else: st.error(msg)

# ==========================================
# PESTAÑA 3: DOCUMENTO SOPORTE (DS)
# ==========================================
with tab3:
    if not can_cause: st.warning("🔒 No tienes permisos para causar en Siigo.")
    else:
        st.subheader("📄 Nuevo Documento Soporte Electrónico (DS)")
        ds_aprobados = db_get_docs(curr_tenant_nit, "DS", "Aprobado")
        if not ds_aprobados: st.info("🎉 No hay Documentos Soporte aprobados pendientes por causar.")
        else:
            terceros_lista = maestros.get("terceros_lista", [])
            types_ds = maestros.get("doc_types_ds", [{"id": 25872, "nombre": "DS - 1 - Doc. Soporte Exterior (ID: 25872)"}])
            prods_lista = maestros.get("productos", [])
            list_iva, list_rete, list_reteiva, list_ica = maestros.get("impuestos_iva", []), maestros.get("impuestos_rete", []), maestros.get("impuestos_reteiva", []), maestros.get("impuestos_ica", [])
            engine = PredictiveEngine(maestros)

            for idx, ds in enumerate(ds_aprobados):
                llave_ds = f"DS_{ds['documento_ref']}"
                with st.container(border=True):
                    f1, f2, f3, f4 = st.columns([2, 1.8, 1.8, 1.5])
                    with f1:
                        dt_sel = st.selectbox("Tipo DS Siigo", options=[t["nombre"] for t in types_ds], key=f"ds_dt_{idx}")
                        id_type_ds = next((t["id"] for t in types_ds if t["nombre"] == dt_sel), 25872)
                        idx_terc_def = buscar_indice_tercero(ds['proveedor'], ds['nit'], terceros_lista)
                        opciones_terc = terceros_lista.copy()
                        if idx_terc_def < 0:
                            opciones_terc.insert(0, f"⚠️ TERCERO NO CREADO EN SIIGO ({ds['nit']} - {ds['proveedor']})")
                            tercero_sel = st.selectbox("Proveedores", options=opciones_terc, index=0, key=f"ds_terc_{idx}")
                        else: tercero_sel = st.selectbox("Proveedores", options=opciones_terc, index=idx_terc_def, key=f"ds_terc_{idx}")
                        if "⚠️" not in tercero_sel: nit_ingresado, prov_nombre = tercero_sel.split(" - ")[0].strip(), tercero_sel.split(" - ", 1)[1].strip()
                        else: nit_ingresado, prov_nombre = ds['nit'], ds['proveedor']
                    with f2:
                        fecha_ds = st.text_input("Fecha", value=ds['fecha'], key=f"ds_fec_{idx}")
                        doc_ref = st.text_input("No. Comprobante", value=re.sub(r'\D', '', str(ds['documento_ref'])), key=f"ds_ref_{idx}")
                    with f3:
                        cc_opts_ds = ["-- Sin Centro de Costo --"] + [c["nombre"] for c in cc_lista]
                        cc_header_sel = st.selectbox("Centro Costo", options=cc_opts_ds, index=cc_opts_ds.index(ds.get("centro_costo")) if ds.get("centro_costo") in cc_opts_ds else 0, key=f"ds_cc_head_{idx}")
                        id_cc_head = next((c["id"] for c in cc_lista if c["nombre"] == cc_header_sel), None) if cc_header_sel != "-- Sin Centro de Costo --" else None
                        moneda_sel = st.selectbox("Moneda", options=["USD", "COP"], index=0 if ds['moneda_origen']=="USD" else 1, key=f"ds_mon_{idx}")
                    with f4:
                        monto_orig = st.number_input("Monto Origen", value=float(ds['monto_origen']), key=f"ds_monto_{idx}")
                        trm_val = st.number_input("TRM", value=float(ds['trm']), key=f"ds_trm_{idx}") if moneda_sel=="USD" else 1.0
                        if moneda_sel == "USD": st.caption(f"💵 **Base COP (TRM):** ${round(monto_orig * trm_val, 2):,.2f}")

                    st.markdown("<div class='siigo-table-header'># | Tipo | Código / Producto | Descripción | Cant | Vr. Unitario | Imp. Cargo (IVA) | Imp. Retención | Valor Total | Acciones</div>", unsafe_allow_html=True)
                    if not ds.get("items_custom"):
                        ds["items_custom"] = [{"type": "Account", "code": engine.predict_mapping(prov_nombre, nit_ingresado, "")["puc_code"], "description": f"Servicios Exterior - {prov_nombre}", "quantity": 1.0, "price": monto_orig, "id_iva": 0, "id_rete": 0}]

                    items_ds_siigo, acum_subtotal_ds, acum_iva_ds = [], 0.0, 0.0
                    for item_idx, item in enumerate(ds.get("items_custom", [])):
                        d0, d1, d2, d3, d4, d5, d6, d7, d8, d9 = st.columns([0.3, 0.8, 1.8, 1.7, 0.5, 1.0, 1.3, 1.7, 0.9, 0.4])
                        with d0: st.markdown(f"**{item_idx+1}**")
                        with d1: tipo_item = st.selectbox("Tipo", options=["Account", "Product"], index=0 if item.get("type", "Account")=="Account" else 1, key=f"ds_tp_{llave_ds}_{item_idx}", label_visibility="collapsed")
                        with d2:
                            if tipo_item == "Account":
                                cat_puc = curr_tenant.get('puc', DEFAULT_PUC)
                                puc_sel = st.selectbox("Código PUC", options=cat_puc, index=0, key=f"ds_puc_{llave_ds}_{item_idx}", label_visibility="collapsed")
                                code_item = re.sub(r'[^\d]', '', puc_sel.split(" - ")[0].strip())
                            else:
                                prod_sel = st.selectbox("Producto Siigo", options=prods_lista if prods_lista else ["Sin Productos"], key=f"ds_prod_{llave_ds}_{item_idx}", label_visibility="collapsed")
                                code_item = prod_sel.split(" - ")[0].strip()
                        with d3: desc_val = st.text_input("Descripción", value=item.get('description', f"Servicio - {prov_nombre}"), key=f"ds_desc_{llave_ds}_{item_idx}", label_visibility="collapsed")
                        with d4: cant_val = st.number_input("Cant", value=float(item.get('quantity', 1.0)), key=f"ds_cant_{llave_ds}_{item_idx}", label_visibility="collapsed")
                        with d5: monto_val = st.number_input("Vr. Unitario", value=float(item.get('price', monto_orig)), key=f"ds_val_{llave_ds}_{item_idx}", label_visibility="collapsed")
                        with d6:
                            iva_sel = st.selectbox("Imp. Cargo", options=[i["nombre"] for i in list_iva], index=0, key=f"ds_iva_{llave_ds}_{item_idx}", label_visibility="collapsed")
                            id_iva = next((i["id"] for i in list_iva if i["nombre"] == iva_sel), 0)
                            pct_iva_sel = next((i["porcentaje"] for i in list_iva if i["nombre"] == iva_sel), 0.0)
                        with d7:
                            rete_sel = st.selectbox("Imp. Retención", options=[i["nombre"] for i in list_rete], index=0, key=f"ds_rete_{llave_ds}_{item_idx}", label_visibility="collapsed")
                            id_rete = next((i["id"] for i in list_rete if i["nombre"] == rete_sel), 0)
                        with d8:
                            sub_row = round(monto_val * cant_val, 2); iva_row = round(sub_row * (pct_iva_sel / 100.0), 2)
                            st.markdown(f"**{'$' if moneda_sel=='COP' else 'USD $'}{sub_row + iva_row:,.2f}**")
                        with d9:
                            if len(ds["items_custom"]) > 1 and st.button("🗑️", key=f"btn_del_line_ds_{llave_ds}_{item_idx}"):
                                ds["items_custom"].pop(item_idx); db_save_doc(curr_tenant_nit, ds['documento_ref'], "DS", "Aprobado", ds); st.rerun()

                        acum_subtotal_ds += sub_row; acum_iva_ds += iva_row
                        items_ds_siigo.append({"code": code_item, "type": tipo_item, "description": desc_val, "quantity": cant_val, "price": monto_val, "cost_center": id_cc_head, "id_iva": id_iva, "id_rete": id_rete, "pct_iva": pct_iva_sel})

                    if st.button("➕ Agregar Línea Adicional", key=f"btn_add_line_ds_{idx}"):
                        ds["items_custom"].append({"type": "Account", "code": "51355001", "description": "Línea Adicional Exterior", "quantity": 1.0, "price": 0.0, "id_iva": 0, "id_rete": 0})
                        db_save_doc(curr_tenant_nit, ds['documento_ref'], "DS", "Aprobado", ds); st.rerun()

                    st.markdown("---")
                    b1, b2, b3 = st.columns([2, 1.8, 1.8])
                    with b1:
                        pago_sel = st.selectbox("Forma de pago", options=[p["nombre"] for p in maestros.get("pagos", [{"id": 1, "nombre": "Efectivo / Crédito (ID: 1)"}])], key=f"ds_pago_{idx}")
                        id_pago = next((p["id"] for p in maestros.get("pagos", []) if p["nombre"] == pago_sel), 1)
                    with b2:
                        sel_reteiva_ds = st.selectbox("ReteIVA (Pie)", options=[i["nombre"] for i in list_reteiva], key=f"ds_glob_reteiva_{idx}")
                        id_reteiva_ds = next((i["id"] for i in list_reteiva if i["nombre"] == sel_reteiva_ds), 0)
                    with b3:
                        sel_reteica_ds = st.selectbox("ReteICA (Pie)", options=[i["nombre"] for i in list_ica], key=f"ds_glob_reteica_{idx}")
                        id_reteica_ds = next((i["id"] for i in list_ica if i["nombre"] == sel_reteica_ds), 0)

                    total_enviar_ds = acum_subtotal_ds + acum_iva_ds
                    st.markdown(f"### **Total Neto: {'$' if moneda_sel=='COP' else 'USD $'}{total_enviar_ds:,.2f} {moneda_sel}**")

                    if st.button("🚀 Transmitir Documento Soporte a Siigo", key=f"btn_ds_send_{idx}", type="primary"):
                        if "⚠️" in tercero_sel: st.error("🔴 Selecciona un tercero válido.")
                        else:
                            num_ref_clean = re.sub(r'\D', '', doc_ref) or "101"
                            valid_items_ds = [it for it in items_ds_siigo if it["price"] > 0 or len(items_ds_siigo) == 1]
                            final_items_payload_ds = []
                            for it in valid_items_ds:
                                taxes_list = []
                                if it["id_iva"] and int(it["id_iva"]) > 0: taxes_list.append({"id": int(it["id_iva"])})
                                if it["id_rete"] and int(it["id_rete"]) > 0: taxes_list.append({"id": int(it["id_rete"])})
                                item_dict = {"code": it["code"], "type": it["type"], "description": it["description"], "quantity": it["quantity"], "price": it["price"], "taxes": taxes_list}
                                if it["cost_center"]: item_dict["cost_center"] = it["cost_center"]
                                final_items_payload_ds.append(item_dict)

                            retentions_payload_ds = []
                            if id_reteiva_ds and int(id_reteiva_ds) > 0: retentions_payload_ds.append({"id": int(id_reteiva_ds)})
                            if id_reteica_ds and int(id_reteica_ds) > 0: retentions_payload_ds.append({"id": int(id_reteica_ds)})

                            payload_ds = {
                                "document": {"id": id_type_ds}, "date": fecha_ds, "supplier": {"identification": nit_ingresado, "branch_office": 0},
                                "retentions": retentions_payload_ds, "observations": f"Documento Soporte (Ref: {num_ref_clean})",
                                "items": final_items_payload_ds, "payments": [{"id": id_pago, "value": round(total_enviar_ds, 2), "due_date": fecha_ds}],
                                "supplier_receipt_number": {"prefix": "DS", "number": int(num_ref_clean[-10:])}, "electronic_type": "Electronic", "is_electronic": True
                            }
                            if id_cc_head: payload_ds["cost_center"] = id_cc_head
                            if moneda_sel == "USD": payload_ds["currency"] = {"code": "USD", "exchange_rate": float(trm_val)}

                            exito, msg, doc_id_siigo, doc_num_siigo = causar_en_siigo_api(payload_ds, is_ds=True)
                            if exito:
                                siigo_ref_completa = f"{doc_num_siigo}|||{doc_id_siigo}"
                                ds["subtotal"] = acum_subtotal_ds
                                ds["iva"] = acum_iva_ds
                                ds["total"] = total_enviar_ds
                                ds["UsuarioCausador"] = curr_user["email"]
                                ds["FechaCausacion"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                                db_save_history(curr_tenant_nit, ds['documento_ref'], "DS", fecha_ds, total_enviar_ds, moneda_sel, siigo_ref_completa, prov_nombre, nit_ingresado, ds.get("pdf_b64"), json.dumps(ds), curr_user["email"])
                                st.toast(f"✅ Causada exitosamente: {doc_num_siigo}", icon="🎉"); st.rerun()
                            else: st.error(msg)

# ==========================================
# PESTAÑA 4: TABLERO DE AUDITORÍA (HISTÓRICO)
# ==========================================
with tab4:
    st.subheader("📊 Histórico de Causaciones (Persistente)")
    hist = db_get_history(curr_tenant_nit)
    if not hist: st.info("No hay documentos causados en la base de datos para esta empresa.")
    else:
        for idx, c in enumerate(hist):
            raw_siigo_str = c.get('id_siigo_num', '') or ''
            num_display, uuid_siigo = raw_siigo_str.split("|||", 1) if "|||" in raw_siigo_str else (raw_siigo_str, raw_siigo_str)
            with st.container(border=True):
                c1, c2, c3, c4 = st.columns([2, 2.5, 1.5, 2])
                with c1: 
                    st.markdown(f"⚡ <span class='badge-siigo'>{num_display}</span>", unsafe_allow_html=True)
                    st.caption(f"Ref: {c['tipo']} #{c['id_doc_prov']} | Fecha: {c['fecha']}")
                with c2: st.markdown(f"**{c['proveedor']}**"); st.caption(f"NIT: {c['nit']}")
                with c3: st.markdown(f"**Total:** {'$' if c['moneda'] == 'COP' else 'USD $'}{c['total']:,.2f}")
                with c4:
                    cb1, cb2 = st.columns(2)
                    with cb1:
                        if c["pdf_original"]: st.download_button("📄 PDF Origen", data=c["pdf_original"], file_name=f"Orig_{c['id_doc_prov']}.pdf", key=f"dl_o_{idx}")
                    with cb2:
                        if uuid_siigo and st.button("⚡ PDF Siigo", key=f"dl_s_{idx}"):
                            pdf = obtener_pdf_siigo_api(uuid_siigo, is_ds=(c["tipo"]=="DS"))
                            if pdf: st.download_button("Descargar PDF", data=pdf, file_name=f"Siigo_{c['id_doc_prov']}.pdf", key=f"btn_dl_pdf_siigo_{idx}")
                            else: st.error("No se pudo obtener el PDF de Siigo.")

# ==========================================
# PESTAÑA 5: REPORTES Y EXPORTACIONES
# ==========================================
with tab_rep:
    st.subheader("📈 Reportes y Exportaciones a Excel")
    st.caption("Descarga informes detallados con desglose de Subtotal, IVA y Total por ítem.")

    col_rep1, col_rep2 = st.columns(2)
    
    with col_rep1:
        st.markdown("##### ⏳ Documentos Aprobados (Por Causar)")
        docs_aprobados = db_get_docs(curr_tenant_nit, "FC", "Aprobado") + db_get_docs(curr_tenant_nit, "DS", "Aprobado")
        if not docs_aprobados:
            st.info("No hay documentos aprobados pendientes.")
        else:
            filas_aprob = []
            for d in docs_aprobados:
                v = extraer_valores_reporte(d)
                filas_aprob.append({
                    "Tipo": v["tipo"],
                    "No. Documento": v["doc_ref"],
                    "Fecha Emisión": v["fecha"],
                    "NIT": v["nit"],
                    "Proveedor": v["proveedor"],
                    "Conceptos": v["conceptos"],
                    "Moneda": v["moneda"],
                    "Subtotal": v["subtotal"],
                    "IVA": v["iva"],
                    "Total": v["total"],
                    "Aprobado Por": v["aprobador"],
                    "Fecha Aprobación": v["fecha_aprobacion"]
                })
            
            df_aprob = pd.DataFrame(filas_aprob)
            st.dataframe(df_aprob, use_container_width=True, height=220)
            st.download_button("📥 Descargar Excel (Aprobados)", data=generar_excel(df_aprob), file_name=f"Reporte_Aprobados_{curr_tenant_nit}.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    with col_rep2:
        st.markdown("##### ⚡ Documentos Causados en Siigo")
        historial = db_get_history(curr_tenant_nit)
        if not historial:
            st.info("No hay documentos causados.")
        else:
            filas_caus = []
            for h in historial:
                data_j = json.loads(h.get("data_json") or "{}")
                v = extraer_valores_reporte(data_j, h_total=h["total"]) if data_j else {
                    "tipo": h["tipo"], "doc_ref": h["id_doc_prov"], "fecha": h["fecha"], "nit": h["nit"],
                    "proveedor": h["proveedor"], "conceptos": f"Causación {h['tipo']}", "moneda": h["moneda"],
                    "subtotal": h["total"], "iva": 0.0, "total": h["total"], "causador": h["usuario"] or "N/A", "fecha_causacion": h["fecha"]
                }
                
                filas_caus.append({
                    "Tipo": v["tipo"],
                    "Ref. Proveedor": v["doc_ref"],
                    "ID Comprobante Siigo": h["id_siigo_num"].split("|||")[0] if "|||" in h["id_siigo_num"] else h["id_siigo_num"],
                    "Fecha": v["fecha"],
                    "NIT": v["nit"],
                    "Proveedor": v["proveedor"],
                    "Conceptos": v["conceptos"],
                    "Moneda": v["moneda"],
                    "Subtotal": v["subtotal"],
                    "IVA": v["iva"],
                    "Total": v["total"],
                    "Causado Por": h["usuario"] or v["causador"],
                    "Fecha Causación": v["fecha_causacion"]
                })
            
            df_caus = pd.DataFrame(filas_caus)
            st.dataframe(df_caus, use_container_width=True, height=220)
            st.download_button("📥 Descargar Excel (Causados)", data=generar_excel(df_caus), file_name=f"Reporte_Causados_{curr_tenant_nit}.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

# ==========================================
# PESTAÑA 6: CONFIGURACIÓN MULTI-EMPRESA & USUARIOS
# ==========================================
with tab_tenant:
    if not can_admin:
        st.warning("🔒 Acceso denegado. Solo administradores.")
    else:
        st.subheader("⚙️ Configuración de Empresa y Gestión Multi-Empresa")
        
        if curr_rol == "SuperAdmin":
            with st.expander("🏢 Crear Nueva Empresa (Tenant SaaS)", expanded=False):
                with st.form("form_create_tenant"):
                    st.caption("Registra una nueva compañía en la plataforma:")
                    new_t_nit = st.text_input("NIT de la Empresa")
                    new_t_razon = st.text_input("Razón Social")
                    new_t_user = st.text_input("Correo Usuario API Siigo")
                    new_t_key = st.text_input("Access Key API Siigo", type="password")
                    
                    if st.form_submit_button("🚀 Crear Empresa en AutoCount"):
                        if new_t_nit and new_t_razon:
                            conn = get_db_connection()
                            try:
                                conn.execute("INSERT INTO tenants VALUES (?,?,?,?,?)", (new_t_nit.strip(), new_t_razon.strip(), new_t_user.strip(), new_t_key.strip(), json.dumps(DEFAULT_PUC)))
                                default_user_email = f"admin@{new_t_nit}.com"
                                conn.execute("INSERT INTO users VALUES (?,?,?,?,?)", (default_user_email, "123456", f"Admin {new_t_razon}", new_t_nit.strip(), "Administrador"))
                                conn.commit()
                                st.success(f"✅ Empresa {new_t_razon} creada con éxito.")
                                st.rerun()
                            except Exception as e:
                                st.error(f"❌ Error al crear empresa: {e}")
                            finally: conn.close()
                        else: st.error("⚠️ Ingrese el NIT y la Razón Social.")

        st.markdown("---")
        if can_config:
            with st.form("form_tenant"):
                st.markdown(f"#### Ficha de Empresa Activa: **{curr_tenant['razon_social']}**")
                t_nit = st.text_input("NIT", value=curr_tenant['nit'], disabled=True)
                t_raz = st.text_input("Razón Social", value=curr_tenant['razon_social'])
                t_usr = st.text_input("Siigo User", value=curr_tenant['siigo_user'])
                t_key = st.text_input("Siigo Key", value=curr_tenant['siigo_key'], type="password")
                if st.form_submit_button("Guardar Cambios Empresa"):
                    conn = get_db_connection()
                    conn.execute("UPDATE tenants SET razon_social=?, siigo_user=?, siigo_key=? WHERE nit=?", (t_raz, t_usr, t_key, t_nit))
                    conn.commit(); conn.close()
                    st.success("Configuración actualizada."); st.rerun()

        st.markdown("---")
        col_u1, col_u2 = st.columns([1.5, 2])
        
        with col_u1:
            st.markdown("#### 👥 Registrar Usuario")
            st.caption("Puedes crear **ilimitados usuarios** por empresa:")
            with st.form("form_user"):
                u_email = st.text_input("Correo Electrónico")
                u_pass = st.text_input("Contraseña", type="password")
                u_name = st.text_input("Nombre Completo")
                u_rol = st.selectbox("Rol Asignado", ["Administrativo", "Auxiliar Administrativo", "Asistente Contable", "Administrador"])
                if st.form_submit_button("➕ Crear Usuario"):
                    if u_email and u_pass:
                        conn = get_db_connection()
                        try:
                            conn.execute("INSERT INTO users VALUES (?,?,?,?,?)", (u_email.lower().strip(), u_pass, u_name, curr_tenant_nit, u_rol))
                            conn.commit()
                            st.success(f"✅ Usuario {u_name} registrado.")
                            st.rerun()
                        except Exception:
                            st.error("❌ El correo ya está registrado.")
                        finally:
                            conn.close()
                    else:
                        st.error("Complete el correo y la contraseña.")

        with col_u2:
            st.markdown("#### 📋 Usuarios Existentes")
            conn = get_db_connection()
            c = conn.cursor()
            c.execute("SELECT email, nombre, rol FROM users WHERE tenant_nit=?", (curr_tenant_nit,))
            user_rows = c.fetchall()
            conn.close()

            if user_rows:
                df_users = pd.DataFrame(user_rows, columns=["Correo Electrónico", "Nombre Completo", "Rol"])
                st.dataframe(df_users, use_container_width=True)
            else:
                st.info("No hay usuarios registrados para esta empresa.")
