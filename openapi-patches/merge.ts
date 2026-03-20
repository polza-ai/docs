/**
 * Скрипт применения патчей к OpenAPI документации
 *
 * Запуск: yarn docs:openapi:patch
 *
 * Читает все .json файлы из текущей директории и мержит их в docs/openapi.json.
 * Поддерживаемые поля патча: paths, components.schemas
 */

import { readFileSync, readdirSync, writeFileSync } from 'fs';
import { join } from 'path';

interface PatchFile {
  paths?: Record<string, unknown>;
  components?: {
    schemas?: Record<string, unknown>;
    [key: string]: unknown;
  };
}

interface OpenAPIDocument {
  paths?: Record<string, unknown>;
  components?: {
    schemas?: Record<string, unknown>;
    [key: string]: unknown;
  };
  [key: string]: unknown;
}

/**
 * Применяет все патчи из директории к файлу openapi.json
 */
export function applyPatches(openapiPath: string): void {
  const doc = JSON.parse(readFileSync(openapiPath, 'utf-8')) as OpenAPIDocument;

  const patchFiles = readdirSync(__dirname)
    .filter((f) => f.endsWith('.json'))
    .sort();

  if (patchFiles.length === 0) {
    console.log('Патчи не найдены, пропуск');
    return;
  }

  let appliedCount = 0;

  for (const fileName of patchFiles) {
    const patchPath = join(__dirname, fileName);
    const patch = JSON.parse(readFileSync(patchPath, 'utf-8')) as PatchFile;

    if (patch.paths) {
      doc.paths = { ...(doc.paths ?? {}), ...patch.paths };
    }

    if (patch.components?.schemas) {
      if (!doc.components) {
        doc.components = {};
      }
      doc.components.schemas = {
        ...(doc.components.schemas ?? {}),
        ...patch.components.schemas,
      };
    }

    appliedCount++;
    console.log(`  ✓ Применён патч: ${fileName}`);
  }

  writeFileSync(openapiPath, JSON.stringify(doc, null, 2));
  console.log(`\nПрименено патчей: ${appliedCount}`);
}

// Standalone запуск
if (require.main === module) {
  const openapiPath = join(__dirname, '..', 'openapi.json');
  applyPatches(openapiPath);
}
