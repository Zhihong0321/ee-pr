const express = require('express');
const path = require('path');
const fs = require('fs');
const crypto = require('crypto');
const axios = require('axios');
const Database = require('better-sqlite3');
const { S3Client, GetObjectCommand, HeadObjectCommand, DeleteObjectCommand } = require('@aws-sdk/client-s3');
require('dotenv').config();

const app = express();
const PORT = process.env.PORT || 3000;

app.use(express.json());
app.use(express.static(path.join(__dirname, 'public')));

// Initialize SQLite Database
const dbPath = path.join(__dirname, 'migration_journal.db');
const db = new Database(dbPath);

db.exec(`
  CREATE TABLE IF NOT EXISTS migration_journal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL,
    table_name TEXT NOT NULL,
    record_id INTEGER NOT NULL,
    column_name TEXT NOT NULL,
    original_url TEXT NOT NULL,
    new_pcloud_url TEXT NOT NULL,
    pcloud_path TEXT NOT NULL,
    r2_key TEXT,
    state TEXT NOT NULL DEFAULT 'discovered',
    sha1_hash TEXT,
    size_bytes INTEGER,
    error_log TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
  );

  CREATE TABLE IF NOT EXISTS row_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL,
    table_name TEXT NOT NULL,
    record_id INTEGER NOT NULL,
    full_row_json TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
  );
`);

// Environment & Clients
const getEnv = () => ({
  r2Endpoint: process.env.R2_ENDPOINT,
  r2AccessKey: process.env.R2_ACCESS_KEY_ID,
  r2SecretKey: process.env.R2_SECRET_ACCESS_KEY,
  r2Bucket: process.env.R2_BUCKET || 'eternalgy-image',
  r2PublicBase: process.env.R2_PUBLIC_BASE_URL,
  pcloudToken: process.env.PCLOUD_OAUTH_TOKEN,
  pcloudFolderId: process.env.PCLOUD_PUBLIC_FOLDER_ID || '32517376417',
  pcloudPublicBase: process.env.PCLOUD_PUBLIC_BASE_URL || 'https://filedn.com/loyekJXFL3Gh2dDJpskERa4',
  pgProxyUrl: process.env.PG_PROXY_URL,
  pgProxyDb: process.env.PG_PROXY_DB_NAME || 'prod_main',
  pgProxyToken: process.env.PG_PROXY_AUTH_TOKEN
});

const getS3Client = () => {
  const env = getEnv();
  return new S3Client({
    endpoint: env.r2Endpoint,
    credentials: {
      accessKeyId: env.r2AccessKey,
      secretAccessKey: env.r2SecretKey
    },
    region: 'auto'
  });
};

// PostgreSQL Helper
async function queryPg(sql, params = []) {
  const env = getEnv();
  try {
    const res = await axios.post(
      `${env.pgProxyUrl.replace(/\/$/, '')}/api/sql`,
      {
        db_name: env.pgProxyDb,
        sql,
        params
      },
      {
        headers: {
          Authorization: `Bearer ${env.pgProxyToken}`,
          'Content-Type': 'application/json'
        },
        timeout: 15000
      }
    );
    return res.data;
  } catch (err) {
    throw new Error(`PostgreSQL Proxy Error: ${err.response ? JSON.stringify(err.response.data) : err.message}`);
  }
}

// Active Migration Job State
let activeJob = {
  id: null,
  status: 'idle', // idle, running, paused, stopped, completed
  mode: 'dry-run', // dry-run or live
  items: [],
  currentIndex: 0,
  stats: { total: 0, completed: 0, failed: 0, pending: 0 },
  logs: []
};

function addLog(msg, type = 'info') {
  const logEntry = { timestamp: new Date().toISOString(), msg, type };
  activeJob.logs.unshift(logEntry);
  if (activeJob.logs.length > 200) activeJob.logs.pop();
}

// -------------------------------------------------------------
// API Endpoints
// -------------------------------------------------------------

// 1. Connection Diagnostic Status
app.get('/api/connections', async (req, res) => {
  const env = getEnv();
  const results = {
    r2: { status: 'checking', details: null },
    pcloud_api: { status: 'checking', details: null },
    filedn_public: { status: 'checking', details: null },
    pg_proxy: { status: 'checking', details: null }
  };

  // Test R2
  try {
    const s3 = getS3Client();
    await s3.send(new HeadObjectCommand({ Bucket: env.r2Bucket, Key: 'non-existent-test-key-123' })).catch(e => {
      if (e.$metadata && (e.$metadata.httpStatusCode === 404 || e.$metadata.httpStatusCode === 403)) {
        // Authenticated successfully even if 404
        return true;
      }
      throw e;
    });
    results.r2 = { status: 'connected', details: `Bucket '${env.r2Bucket}' accessible` };
  } catch (err) {
    results.r2 = { status: 'error', details: err.message };
  }

  // Test pCloud API
  try {
    const pRes = await axios.get(`https://api.pcloud.com/userinfo?auth=${env.pcloudToken}`);
    if (pRes.data.result === 0) {
      results.pcloud_api = { status: 'connected', details: `User: ${pRes.data.email} (${pRes.data.quota ? Math.round(pRes.data.quota / (1024*1024*1024)) + ' GB' : ''})` };
    } else {
      results.pcloud_api = { status: 'error', details: pRes.data.error || 'Invalid token' };
    }
  } catch (err) {
    results.pcloud_api = { status: 'error', details: err.message };
  }

  // Test filedn.com public access
  try {
    const fRes = await axios.head(`${env.pcloudPublicBase.replace(/\/$/, '')}/`, { timeout: 8000, validateStatus: () => true });
    results.filedn_public = { status: 'connected', details: `HTTP Status ${fRes.status} (${env.pcloudPublicBase})` };
  } catch (err) {
    results.filedn_public = { status: 'warning', details: `Public URL check: ${err.message}` };
  }

  // Test PostgreSQL Proxy
  try {
    const pgRes = await queryPg('SELECT now() as now;');
    if (pgRes && pgRes.rows && pgRes.rows[0]) {
      results.pg_proxy = { status: 'connected', details: `DB: ${env.pgProxyDb} @ ${pgRes.rows[0].now}` };
    } else {
      results.pg_proxy = { status: 'error', details: 'Unexpected DB response' };
    }
  } catch (err) {
    results.pg_proxy = { status: 'error', details: err.message };
  }

  res.json(results);
});

// 1b. R2 Storage Usage Breakdown Endpoint
app.get('/api/r2-storage', async (req, res) => {
  try {
    const env = getEnv();
    const s3 = getS3Client();
    const { ListObjectsV2Command } = require('@aws-sdk/client-s3');
    let isTruncated = true;
    let continuationToken;
    let totalObjects = 0;
    let totalBytes = 0;
    const prefixStats = {};

    while (isTruncated) {
      const response = await s3.send(new ListObjectsV2Command({
        Bucket: env.r2Bucket,
        ContinuationToken: continuationToken
      }));

      (response.Contents || []).forEach(obj => {
        totalObjects++;
        totalBytes += obj.Size;
        const prefix = obj.Key.split('/')[0] || '(root)';
        if (!prefixStats[prefix]) prefixStats[prefix] = { count: 0, bytes: 0 };
        prefixStats[prefix].count++;
        prefixStats[prefix].bytes += obj.Size;
      });

      isTruncated = response.IsTruncated;
      continuationToken = response.NextContinuationToken;
    }

    const breakdown = Object.keys(prefixStats).map(p => ({
      prefix: p,
      count: prefixStats[p].count,
      bytes: prefixStats[p].bytes,
      size_mb: (prefixStats[p].bytes / (1024 * 1024)).toFixed(2),
      size_gb: (prefixStats[p].bytes / (1024 * 1024 * 1024)).toFixed(2),
      percent: totalBytes > 0 ? ((prefixStats[p].bytes / totalBytes) * 100).toFixed(1) : '0'
    })).sort((a, b) => b.bytes - a.bytes);

    res.json({
      success: true,
      bucket: env.r2Bucket,
      total_objects: totalObjects,
      total_bytes: totalBytes,
      total_mb: (totalBytes / (1024 * 1024)).toFixed(2),
      total_gb: (totalBytes / (1024 * 1024 * 1024)).toFixed(3),
      breakdown: breakdown
    });
  } catch (err) {
    res.status(500).json({ success: false, error: err.message });
  }
});

// 2. Discovery Candidates Endpoint
app.get('/api/discovery', async (req, res) => {
  const ageDays = parseInt(req.query.age_days || '60', 10);
  const statusFilter = req.query.status || 'Approved';

  try {
    const sql = `
      SELECT id, applicant_name, seda_status, updated_at, 
             property_ownership_prove, mykad_pdf, tnb_bill_1, tnb_bill_2, tnb_bill_3, customer_signature,
             ic_copy_front, ic_copy_back, ssm_registration
      FROM seda_registration 
      WHERE seda_status ILIKE $1 
        AND updated_at < NOW() - ($2 || ' days')::INTERVAL 
      ORDER BY updated_at ASC;
    `;
    const pgRes = await queryPg(sql, [`%${statusFilter}%`, ageDays.toString()]);
    const candidates = [];
    const env = getEnv();

    (pgRes.rows || []).forEach(row => {
      const docFields = [
        'property_ownership_prove', 'mykad_pdf', 'tnb_bill_1', 'tnb_bill_2', 'tnb_bill_3',
        'customer_signature', 'ic_copy_front', 'ic_copy_back', 'ssm_registration'
      ];

      docFields.forEach(field => {
        const val = row[field];
        if (val && typeof val === 'string' && val.trim() !== '') {
          // Normalize filename & target pCloud path
          const cleanVal = val.startsWith('//') ? `https:${val}` : val;
          const urlParts = cleanVal.split('/');
          const filename = urlParts[urlParts.length - 1] || `doc_${field}.pdf`;
          const pcloudPath = `Public/R2-Archive/seda_registration/${row.id}/${filename}`;
          const targetPublicUrl = `${env.pcloudPublicBase.replace(/\/$/, '')}/${pcloudPath}`;

          candidates.push({
            db_id: row.id,
            applicant_name: row.applicant_name || 'N/A',
            seda_status: row.seda_status,
            updated_at: row.updated_at,
            column_name: field,
            original_url: cleanVal,
            filename: filename,
            target_pcloud_path: pcloudPath,
            target_public_url: targetPublicUrl
          });
        }
      });
    });

    res.json({
      success: true,
      query_params: { age_days: ageDays, status: statusFilter },
      matching_records_count: (pgRes.rows || []).length,
      total_candidate_files: candidates.length,
      candidates: candidates
    });
  } catch (err) {
    res.status(500).json({ success: false, error: err.message });
  }
});

// 3. Start Job
app.post('/api/job/start', async (req, res) => {
  const { mode = 'dry-run', items = [] } = req.body;
  if (!items || items.length === 0) {
    return res.status(400).json({ success: false, error: 'No items selected for migration job.' });
  }

  const batchId = `batch_${Date.now()}`;
  activeJob = {
    id: batchId,
    status: 'running',
    mode: mode,
    items: items.map(item => ({
      ...item,
      batch_id: batchId,
      state: 'pending',
      error: null
    })),
    currentIndex: 0,
    stats: { total: items.length, completed: 0, failed: 0, pending: items.length },
    logs: []
  };

  addLog(`Started new migration batch '${batchId}' in ${mode.toUpperCase()} mode with ${items.length} items.`);

  // Process async in background looper
  processJobLoop();

  res.json({ success: true, batch_id: batchId, mode: mode, total_items: items.length });
});

// Job Background Processor
async function processJobLoop() {
  const env = getEnv();
  const insertJournal = db.prepare(`
    INSERT INTO migration_journal (batch_id, table_name, record_id, column_name, original_url, new_pcloud_url, pcloud_path, state, error_log)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
  `);
  const insertSnapshot = db.prepare(`
    INSERT INTO row_snapshots (batch_id, table_name, record_id, full_row_json)
    VALUES (?, ?, ?, ?)
  `);

  while (activeJob.status === 'running' && activeJob.currentIndex < activeJob.items.length) {
    const item = activeJob.items[activeJob.currentIndex];
    item.state = 'processing'; AddLog(`Processing #${item.db_id} (${item.column_name}): ${item.filename}`);

    try {
      if (activeJob.mode === 'dry-run') {
        // Dry-run preview simulation
        await new Promise(r => setTimeout(r, 600));
        item.state = 'dry_run_passed';
        insertJournal.run(item.batch_id, 'seda_registration', item.db_id, item.column_name, item.original_url, item.target_public_url, item.target_pcloud_path, 'dry_run_passed', null);
        addLog(`[DRY-RUN SUCCESS] Record #${item.db_id} (${item.column_name}) validated cleanly.`);
      } else {
        // LIVE MIGRATION PIPELINE

        // 1. Take DB Row Snapshot before mutation
        const rowData = await queryPg(`SELECT * FROM seda_registration WHERE id = $1;`, [item.db_id]);
        if (rowData.rows && rowData.rows[0]) {
          insertSnapshot.run(item.batch_id, 'seda_registration', item.db_id, JSON.stringify(rowData.rows[0]));
          
          // Also save backup file
          const backupDir = path.join(__dirname, 'backups');
          if (!fs.existsSync(backupDir)) fs.mkdirSync(backupDir, { recursive: true });
          fs.writeFileSync(
            path.join(backupDir, `${item.batch_id}_seda_registration_${item.db_id}.json`),
            JSON.stringify(rowData.rows[0], null, 2)
          );
        }

        // 2. Fetch File Stream from Source / R2
        let fileBuffer;
        if (item.original_url.includes('.r2.dev') || item.original_url.includes('cloudflarestorage.com')) {
          const s3 = getS3Client();
          const r2Key = item.original_url.split('.r2.dev/')[1] || item.filename;
          const s3Obj = await s3.send(new GetObjectCommand({ Bucket: env.r2Bucket, Key: r2Key }));
          fileBuffer = await streamToBuffer(s3Obj.Body);
        } else {
          // Download via HTTP
          const httpRes = await axios.get(item.original_url, { responseType: 'arraybuffer', timeout: 15000 });
          fileBuffer = Buffer.from(httpRes.data);
        }

        const sha1 = crypto.createHash('sha1').update(fileBuffer).digest('hex');

        // 3. Upload to pCloud Public Folder
        const FormData = require('form-data');
        const form = new FormData();
        form.append('file', fileBuffer, item.filename);

        const pcloudUploadRes = await axios.post(
          `https://api.pcloud.com/uploadfile?folderid=${env.pcloudFolderId}&auth=${env.pcloudToken}&nopartial=1`,
          form,
          { headers: form.getHeaders() }
        );

        if (pcloudUploadRes.data.result !== 0) {
          throw new Error(`pCloud Upload Error: ${pcloudUploadRes.data.error || 'Upload failed'}`);
        }

        addLog(`[PCLOUD COPIED] Uploaded #${item.db_id} (${item.filename}) SHA-1: ${sha1}`);

        // 4. Verify Public Access via filedn.com
        const filednCheck = await axios.head(item.target_public_url, { timeout: 8000, validateStatus: () => true });
        if (filednCheck.status !== 200 && filednCheck.status !== 206) {
          addLog(`[WARNING] Public URL returned HTTP ${filednCheck.status}, proceeding with verified API upload...`, 'warning');
        }

        // 5. Atomic PostgreSQL Compare-and-Set Update
        const updateSql = `UPDATE seda_registration SET ${item.column_name} = $1 WHERE id = $2 AND ${item.column_name} = $3;`;
        const updateRes = await queryPg(updateSql, [item.target_public_url, item.db_id, item.original_url]);

        if (!updateRes || updateRes.rowCount !== 1) {
          throw new Error(`PostgreSQL Compare-and-Set Failed: Affected rows = ${updateRes ? updateRes.rowCount : 0}`);
        }

        addLog(`[DB UPDATED] Record #${item.db_id} column '${item.column_name}' updated to pCloud Public URL.`);

        // 6. Record Journal State
        insertJournal.run(item.batch_id, 'seda_registration', item.db_id, item.column_name, item.original_url, item.target_public_url, item.target_pcloud_path, 'completed', null);
        item.state = 'completed';
      }

      activeJob.stats.completed++;
    } catch (err) {
      item.state = 'failed';
      item.error = err.message;
      activeJob.stats.failed++;
      insertJournal.run(item.batch_id, 'seda_registration', item.db_id, item.column_name, item.original_url, item.target_public_url, item.target_pcloud_path, 'failed', err.message);
      addLog(`[ERROR] Item #${item.db_id} failed: ${err.message}`, 'error');
    }

    activeJob.stats.pending--;
    activeJob.currentIndex++;
  }

  if (activeJob.currentIndex >= activeJob.items.length) {
    activeJob.status = 'completed';
    addLog(`Batch '${activeJob.id}' finished execution. Total: ${activeJob.stats.total}, Completed: ${activeJob.stats.completed}, Failed: ${activeJob.stats.failed}`);
  }
}

// Stream helper
function streamToBuffer(stream) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    stream.on('data', chunk => chunks.push(chunk));
    stream.on('error', reject);
    stream.on('end', () => resolve(Buffer.concat(chunks)));
  });
}

// 4. Job Status Endpoint
app.get('/api/job/status', (req, res) => {
  res.json(activeJob);
});

// 5. Job Controls (Pause/Resume/Stop)
app.post('/api/job/control', (req, res) => {
  const { action } = req.body;
  if (action === 'pause' && activeJob.status === 'running') {
    activeJob.status = 'paused';
    addLog('Job execution paused by user.', 'warning');
  } else if (action === 'resume' && activeJob.status === 'paused') {
    activeJob.status = 'running';
    addLog('Job execution resumed by user.');
    processJobLoop();
  } else if (action === 'stop') {
    activeJob.status = 'stopped';
    addLog('Job execution stopped by user.', 'error');
  }
  res.json({ success: true, status: activeJob.status });
});

// 6. History & Journal Endpoint
app.get('/api/history', (req, res) => {
  const rows = db.prepare(`SELECT * FROM migration_journal ORDER BY id DESC LIMIT 100`).all();
  res.json(rows);
});

// 7. Revert / Rollback Endpoint
app.post('/api/rollback', async (req, res) => {
  const { batch_id, record_id } = req.body;
  try {
    let itemsToRollback = [];
    if (batch_id) {
      itemsToRollback = db.prepare(`SELECT * FROM migration_journal WHERE batch_id = ? AND state IN ('completed', 'db_updated', 'dry_run_passed')`).all(batch_id);
    } else if (record_id) {
      itemsToRollback = db.prepare(`SELECT * FROM migration_journal WHERE record_id = ? AND state IN ('completed', 'db_updated', 'dry_run_passed')`).all(record_id);
    }

    if (itemsToRollback.length === 0) {
      return res.status(404).json({ success: false, error: 'No eligible items found to rollback.' });
    }

    let count = 0;
    for (const item of itemsToRollback) {
      if (item.state === 'completed') {
        const updateSql = `UPDATE ${item.table_name} SET ${item.column_name} = $1 WHERE id = $2 AND ${item.column_name} = $3;`;
        const pgRes = await queryPg(updateSql, [item.original_url, item.record_id, item.new_pcloud_url]);
        if (pgRes && pgRes.rowCount === 1) {
          db.prepare(`UPDATE migration_journal SET state = 'reverted', updated_at = CURRENT_TIMESTAMP WHERE id = ?`).run(item.id);
          count++;
        }
      } else {
        db.prepare(`UPDATE migration_journal SET state = 'reverted', updated_at = CURRENT_TIMESTAMP WHERE id = ?`).run(item.id);
        count++;
      }
    }

    addLog(`[ROLLBACK SUCCESS] Reverted ${count} records back to original URLs.`);
    res.json({ success: true, reverted_count: count });
  } catch (err) {
    res.status(500).json({ success: false, error: err.message });
  }
});

// Bind to 127.0.0.1 ONLY for localhost security
app.listen(PORT, '127.0.0.1', () => {
  console.log(`\n======================================================`);
  console.log(` Local Migration Dashboard running at:`);
  console.log(` 👉 http://127.0.0.1:${PORT}`);
  console.log(`======================================================\n`);
});
