import { inflateRawSync } from "node:zlib";

const END_OF_CENTRAL_DIRECTORY = 0x06054b50;
const CENTRAL_DIRECTORY_ENTRY = 0x02014b50;
const LOCAL_FILE_HEADER = 0x04034b50;
const STORED = 0;
const DEFLATED = 8;

/**
 * One file out of a ZIP archive, or null when the archive has no such name. A ZIP lists its files in a directory at the
 * end, so this reads that directory and then the one file it points to. Enough for the export test, which needs to read
 * `manifest.json` without a ZIP library (the archive is written with the standard library's zipfile: stored or deflated).
 */
export function readZipEntry(archive: Buffer, name: string): Buffer | null {
  let end = archive.length - 22;
  while (end >= 0 && archive.readUInt32LE(end) !== END_OF_CENTRAL_DIRECTORY) end -= 1;
  if (end < 0) throw new Error("not a ZIP archive: no end-of-directory record");

  const entries = archive.readUInt16LE(end + 10);
  let at = archive.readUInt32LE(end + 16);
  for (let seen = 0; seen < entries; seen += 1) {
    if (archive.readUInt32LE(at) !== CENTRAL_DIRECTORY_ENTRY) throw new Error("damaged ZIP directory");
    const method = archive.readUInt16LE(at + 10);
    const compressedSize = archive.readUInt32LE(at + 20);
    const nameLength = archive.readUInt16LE(at + 28);
    const extraLength = archive.readUInt16LE(at + 30);
    const commentLength = archive.readUInt16LE(at + 32);
    const localHeader = archive.readUInt32LE(at + 42);
    const entryName = archive.toString("utf8", at + 46, at + 46 + nameLength);
    at += 46 + nameLength + extraLength + commentLength;
    if (entryName !== name) continue;

    if (archive.readUInt32LE(localHeader) !== LOCAL_FILE_HEADER) throw new Error("damaged ZIP entry");
    const start = localHeader + 30 + archive.readUInt16LE(localHeader + 26) + archive.readUInt16LE(localHeader + 28);
    const data = archive.subarray(start, start + compressedSize); // the sizes come from the directory: a data descriptor leaves the local header's at 0
    if (method === STORED) return data;
    if (method === DEFLATED) return inflateRawSync(data);
    throw new Error(`ZIP compression method ${method} is not supported`);
  }
  return null;
}

/** The names of every file in the archive, in the order the directory lists them. */
export function listZipEntries(archive: Buffer): string[] {
  let end = archive.length - 22;
  while (end >= 0 && archive.readUInt32LE(end) !== END_OF_CENTRAL_DIRECTORY) end -= 1;
  if (end < 0) throw new Error("not a ZIP archive: no end-of-directory record");
  const names: string[] = [];
  let at = archive.readUInt32LE(end + 16);
  for (let seen = 0; seen < archive.readUInt16LE(end + 10); seen += 1) {
    const nameLength = archive.readUInt16LE(at + 28);
    names.push(archive.toString("utf8", at + 46, at + 46 + nameLength));
    at += 46 + nameLength + archive.readUInt16LE(at + 30) + archive.readUInt16LE(at + 32);
  }
  return names;
}
