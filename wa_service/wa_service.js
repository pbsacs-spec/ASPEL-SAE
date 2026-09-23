/**
 * Servicio WhatsApp para Aspel SAE
 * Escanea QR con tu telefono y responde consultas de existencias.
 *
 * Puerto HTTP: 5001  (Flask corre en 5000)
 * Endpoints:
 *   GET /status      → { status: 'waiting_qr' | 'connected' | 'disconnected' }
 *   GET /qr.png      → imagen PNG del QR (solo cuando status = waiting_qr)
 */

const { Client, LocalAuth } = require('whatsapp-web.js');
const qrcode  = require('qrcode');
const express = require('express');
const axios   = require('axios');

const FLASK_URL  = 'http://localhost:5000';
const HTTP_PORT  = 5001;

// ── Estado global ────────────────────────────────────────────────
let qrPngBuffer = null;
let estado      = 'disconnected';   // 'waiting_qr' | 'connected' | 'disconnected'

// ── Cliente WhatsApp ─────────────────────────────────────────────
const { executablePath } = require('puppeteer');

const client = new Client({
    authStrategy: new LocalAuth({ dataPath: './wa_session' }),
    puppeteer: {
        headless: true,
        executablePath: executablePath(),
        args: [
            '--no-sandbox',
            '--disable-setuid-sandbox',
            '--disable-dev-shm-usage',
            '--disable-gpu',
        ],
    },
});

client.on('qr', async (qr) => {
    estado = 'waiting_qr';
    qrPngBuffer = await qrcode.toBuffer(qr, { type: 'png', width: 300, margin: 2 });
    console.log('[WA] QR generado. Abre http://localhost:5001/qr.png o el panel de admin.');
});

client.on('ready', () => {
    estado      = 'connected';
    qrPngBuffer = null;
    const info  = client.info;
    console.log(`[WA] Conectado como ${info.pushname} (${info.wid.user})`);
});

client.on('authenticated', () => {
    console.log('[WA] Sesion autenticada correctamente.');
});

client.on('auth_failure', () => {
    estado = 'disconnected';
    console.error('[WA] Error de autenticacion. Borra la carpeta wa_session y vuelve a escanear.');
});

client.on('disconnected', (reason) => {
    estado      = 'disconnected';
    qrPngBuffer = null;
    console.log('[WA] Desconectado:', reason);
    console.log('[WA] Reconectando en 10s...');
    setTimeout(() => {
        client.initialize().catch(err => console.error('[WA] Error al reinicializar:', err.message));
    }, 10000);
});

// Evitar que errores no capturados (como lockfile en LOGOUT) maten el proceso
process.on('uncaughtException', (err) => {
    if (err.message && err.message.includes('EBUSY')) {
        console.warn('[WA] Advertencia lockfile (ignorada):', err.message);
        return;
    }
    console.error('[WA] Excepcion no capturada:', err.message);
});

process.on('unhandledRejection', (reason) => {
    const msg = reason && reason.message ? reason.message : String(reason);
    const ignorar = [
        'EBUSY',
        'Execution context was destroyed',
        'Session closed',
        'Target closed',
        'Protocol error',
    ];
    if (ignorar.some(e => msg.includes(e))) {
        console.warn('[WA] Advertencia ignorada:', msg.slice(0, 80));
        return;
    }
    console.error('[WA] Promesa rechazada no capturada:', msg);
});

// ── Mensajes entrantes ───────────────────────────────────────────
client.on('message', async (msg) => {
    // Ignorar estados de contactos, broadcasts y mensajes propios
    if (msg.fromMe)                          return;
    if (!msg.body)                           return;
    if (msg.from.endsWith('@broadcast'))     return;
    if (msg.type !== 'chat')                 return;   // solo texto plano

    const texto = msg.body.trim();
    const lower = texto.toLowerCase();

    // Solo procesar mensajes que empiecen con "sae"
    if (!lower.startsWith('sae')) return;
    if (lower.length > 3 && lower[3] !== ' ') return; // evitar "saeta", "saenz", etc.

    const consulta = texto.slice(3).trim() || 'ayuda';
    console.log(`[WA] Mensaje de ${msg.from}: ${consulta}`);

    try {
        const res = await axios.post(
            `${FLASK_URL}/api/wa/procesar`,
            { texto: consulta, de: msg.from },
            { timeout: 30000 }
        );
        const data = res.data;

        // Nota: pdf/excel ya no se mandan como adjunto -- whatsapp-web.js no
        // puede enviar MEDIA a contactos "@lid" (identificador nuevo de
        // WhatsApp; error interno "must include an id property", el texto no
        // se ve afectado). El backend Flask ahora manda un link de descarga
        // como texto normal en vez de un archivo adjunto.
        if (data.respuesta) {
            await client.sendMessage(msg.from, data.respuesta);
            console.log(`[WA] Texto enviado a ${msg.from} (${data.respuesta.length} chars)`);
        } else {
            await client.sendMessage(msg.from, 'Sin respuesta.');
        }
    } catch (err) {
        console.error('[WA] Error al procesar mensaje:', err.message);
        try {
            await client.sendMessage(msg.from, 'Error al consultar. Intenta de nuevo.');
        } catch (sendErr) {
            console.error('[WA] Error al enviar mensaje de error:', sendErr.message);
        }
    }
});

client.initialize();
console.log('[WA] Iniciando cliente WhatsApp...');

// ── Servidor HTTP para el panel de admin ─────────────────────────
const app = express();

app.get('/status', (_req, res) => {
    res.json({ status: estado });
});

app.get('/qr.png', (req, res) => {
    if (!qrPngBuffer) {
        const msg = estado === 'connected' ? 'ya_conectado' : 'qr_no_disponible';
        return res.status(404).json({ error: msg });
    }
    res.set('Content-Type', 'image/png');
    res.set('Cache-Control', 'no-store');
    res.send(qrPngBuffer);
});

app.listen(HTTP_PORT, '127.0.0.1', () => {
    console.log(`[WA] Servicio HTTP escuchando en http://localhost:${HTTP_PORT}`);
});
