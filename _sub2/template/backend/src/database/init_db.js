const fs = require('fs');
const path = require('path');
const sqlite3 = require('sqlite3').verbose();

const DEFAULT_DB_FILENAME = 'database.db';

let db = null;
let initPromise = null;
let currentDbPath = resolveDbPath(
  process.env.ARC_DB_FILE || process.env.DATABASE_FILE || DEFAULT_DB_FILENAME,
);

function resolveDbPath(inputPath = DEFAULT_DB_FILENAME) {
  const candidate = String(inputPath || DEFAULT_DB_FILENAME).trim() || DEFAULT_DB_FILENAME;
  return path.resolve(process.cwd(), candidate);
}

function ensureDbDirectory(filePath) {
  fs.mkdirSync(path.dirname(filePath), { recursive: true });
}

function getDbPath() {
  return currentDbPath;
}

async function setDbPath(nextPath) {
  const resolvedPath = resolveDbPath(nextPath);
  if (resolvedPath === currentDbPath) {
    return currentDbPath;
  }

  await closeDb();
  currentDbPath = resolvedPath;
  return currentDbPath;
}

function getDb() {
  if (!db) {
    ensureDbDirectory(currentDbPath);
    db = new sqlite3.Database(currentDbPath);
  }
  return db;
}

function runStatement(database, sql) {
  return new Promise((resolve, reject) => {
    database.run(sql, (err) => {
      if (err) {
        reject(err);
        return;
      }
      resolve();
    });
  });
}

async function initializeDatabase(options = {}) {
  if (options.dbPath) {
    await setDbPath(options.dbPath);
  }
  if (options.reset) {
    await resetDatabaseFile();
  }

  const database = getDb();
  if (!initPromise) {
    initPromise = (async () => {
      await runStatement(database, 'PRAGMA foreign_keys = ON;');

      /**
       * Guide model instructions:
       * 1. Use CREATE TABLE IF NOT EXISTS to create new tables.
       * 2. When adding fields, use ALTER TABLE ... ADD COLUMN ... and guard it with existence checks or tolerant error handling.
       * 3. Keep schema evolution idempotent and centralized in this file.
       * 4. Reuse `db_runtime.js` for CRUD helpers and `test_harness.js` for test DB lifecycle instead of re-implementing one-off connection logic elsewhere.
       */

      // Auto-run feature migrations from src/migrations/*.sql (additive, conflict-free).
      // Leaves create src/migrations/<node_id>.sql with CREATE TABLE IF NOT EXISTS
      // statements; the compiler merges leaves by copying these files. Do not edit
      // this file to add tables.
      const migrationsDir = path.join(__dirname, 'migrations');
      if (fs.existsSync(migrationsDir)) {
        for (const file of fs.readdirSync(migrationsDir).sort()) {
          if (!file.endsWith('.sql')) continue;
          const sql = fs.readFileSync(path.join(migrationsDir, file), 'utf8');
          await runStatement(database, sql);
        }
      }
    })();
  }

  try {
    await initPromise;
  } catch (error) {
    initPromise = null;
    throw error;
  }

  // Always return the live database handle. Previously this returned
  // `initPromise` on the second call, which resolves to `undefined` and broke
  // callers such as `withTransaction` (TypeError: Cannot read properties of
  // undefined (reading 'exec')).
  return database;
}

function closeDb() {
  if (!db) {
    initPromise = null;
    return Promise.resolve();
  }

  const currentDb = db;
  db = null;
  initPromise = null;
  return new Promise((resolve, reject) => {
    currentDb.close((err) => {
      if (err) {
        reject(err);
        return;
      }
      resolve();
    });
  });
}

async function removeDatabaseFile(targetPath = currentDbPath) {
  const resolvedPath = resolveDbPath(targetPath);
  if (resolvedPath === currentDbPath) {
    await closeDb();
  }
  if (fs.existsSync(resolvedPath)) {
    fs.rmSync(resolvedPath, { force: true });
  }
}

async function resetDatabaseFile(targetPath = currentDbPath) {
  await removeDatabaseFile(targetPath);
}

module.exports = {
  DEFAULT_DB_FILENAME,
  resolveDbPath,
  getDbPath,
  setDbPath,
  getDb,
  initializeDatabase,
  closeDb,
  removeDatabaseFile,
  resetDatabaseFile,
};
