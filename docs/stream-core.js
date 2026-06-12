/*
 * Streaming core for very large courses (PROOF OF CONCEPT)
 *
 * The non-streaming prototype (lti-core.js) holds the whole .imscc and the
 * whole rebuilt zip in memory — fine for small courses, fatal at 600MB+.
 *
 * This version keeps memory flat regardless of course size:
 *
 *   - Canvas .imscc: read with random access, decompress ONLY the small XML
 *     files needed to find LTI links; then stream-copy every entry to the
 *     output, rewriting only XML entries. Media files are piped through
 *     without ever being fully buffered.
 *
 *   - edX .tar.gz: decompress the gzip as a STREAM, parse tar headers on the
 *     fly, keep only the small hierarchy/LTI XML files, and discard the
 *     (large) media bytes as they pass. We read all 600MB sequentially but
 *     never hold more than a chunk + the small XML set.
 *
 * Built on web-standard streams (DecompressionStream, ReadableStream) and
 * zip.js, both available in modern browsers and in Node 18+, so the exact
 * same code is unit-tested headlessly and shipped to the browser.
 *
 * Reuses pure functions from lti-core.js (URL parsing, mapping, CSV) where
 * they don't touch I/O.
 */

(function (root, factory) {
  if (typeof module === 'object' && module.exports) {
    module.exports = factory(require('@zip.js/zip.js'), require('./lti-core.js'));
  } else {
    root.LTIStream = factory(root.zip, root.LTICore);
  }
})(typeof self !== 'undefined' ? self : this, function (zip, LTICore) {
  'use strict';

  const td = new TextDecoder('utf-8');

  // Directories in an OLX export whose XML we DO need (small). Everything
  // else (static/, assets, drafts, policies, media) is skipped while streaming.
  const OLX_KEEP_DIRS = new Set([
    'chapter', 'sequential', 'vertical', 'course',
    'problem', 'html', 'video', 'lti_consumer', 'lti', 'openassessment',
    'discussion', 'library_content', 'conditional', 'split_test',
  ]);

  // Depth-agnostic: keep any course.xml, or any XML whose parent directory is
  // an OLX content dir — regardless of how deep the archive wraps things.
  // The true course root is resolved later in buildOlxResult.
  function olxPathNeeded(name) {
    const parts = name.replace(/^\.\//, '').split('/');
    const base = parts[parts.length - 1];
    if (base.startsWith('._')) return false;
    if (name.includes('__MACOSX')) return false;
    if (!base.endsWith('.xml')) return false;
    if (base === 'course.xml') return true;
    const parent = parts.length >= 2 ? parts[parts.length - 2] : '';
    return OLX_KEEP_DIRS.has(parent);
  }

  // ------------------------------------------------------------------
  // Streaming tar.gz scanner: yields only the small XML entries we need.
  // Memory stays at ~one chunk + the kept XML files (a few MB total).
  // ------------------------------------------------------------------
  async function scanOlxStream(readable) {
    // Decompress gzip as a stream
    const gunzipped = readable.pipeThrough(new DecompressionStream('gzip'));
    const reader = gunzipped.getReader();

    const kept = new Map(); // tar path -> string (XML content)
    let leftover = new Uint8Array(0);

    // tar parsing state
    let mode = 'header';      // 'header' | 'skip' | 'collect'
    let remaining = 0;        // bytes left in current entry body
    let padding = 0;          // bytes of 512-block padding after body
    let collectBuf = null;    // Uint8Array when collecting a wanted file
    let collectOff = 0;
    let pendingName = null;
    let paxPath = null;
    let gnuLongName = null;

    const concat = (a, b) => {
      const out = new Uint8Array(a.length + b.length);
      out.set(a, 0); out.set(b, a.length);
      return out;
    };
    const cstr = (buf, start, len) => {
      let end = start;
      const max = start + len;
      while (end < max && buf[end] !== 0) end++;
      return td.decode(buf.subarray(start, end));
    };

    function handleHeader(block) {
      // All-zero block => end of archive
      let allZero = true;
      for (let i = 0; i < 512; i++) if (block[i] !== 0) { allZero = false; break; }
      if (allZero) return false;

      let name = cstr(block, 0, 100);
      const size = parseInt((cstr(block, 124, 12).trim() || '0'), 8) || 0;
      const type = String.fromCharCode(block[156] || 48);
      const prefix = cstr(block, 345, 155);
      if (prefix) name = prefix + '/' + name;

      const bodyPad = Math.ceil(size / 512) * 512 - size;

      if (type === 'x') {
        // pax header — collect to parse 'path='
        mode = 'collect'; pendingName = '\0pax'; remaining = size; padding = bodyPad;
        collectBuf = new Uint8Array(size); collectOff = 0;
      } else if (type === 'L') {
        mode = 'collect'; pendingName = '\0gnu'; remaining = size; padding = bodyPad;
        collectBuf = new Uint8Array(size); collectOff = 0;
      } else if (type === '0' || type === '\0' || type === '7') {
        const finalName = (paxPath || gnuLongName || name).replace(/^\.\//, '');
        paxPath = null; gnuLongName = null;
        if (olxPathNeeded(finalName)) {
          mode = 'collect'; pendingName = finalName; remaining = size; padding = bodyPad;
          collectBuf = new Uint8Array(size); collectOff = 0;
        } else {
          mode = 'skip'; remaining = size; padding = bodyPad;
        }
      } else {
        // directory / link — no body to speak of, but honor size/padding
        mode = 'skip'; remaining = size; padding = bodyPad;
      }
      return true;
    }

    function finishCollect() {
      if (pendingName === '\0pax') {
        const s = td.decode(collectBuf);
        let p = 0;
        while (p < s.length) {
          const sp = s.indexOf(' ', p);
          if (sp < 0) break;
          const len = parseInt(s.slice(p, sp), 10);
          if (!len) break;
          const rec = s.slice(sp + 1, p + len - 1);
          const eq = rec.indexOf('=');
          if (eq > 0 && rec.slice(0, eq) === 'path') paxPath = rec.slice(eq + 1);
          p += len;
        }
      } else if (pendingName === '\0gnu') {
        gnuLongName = td.decode(collectBuf).replace(/\0+$/, '');
      } else {
        kept.set(pendingName, td.decode(collectBuf));
      }
      collectBuf = null; pendingName = null;
    }

    // Main pump
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      let buf = leftover.length ? concat(leftover, value) : value;
      let pos = 0;

      for (;;) {
        if (mode === 'header') {
          if (buf.length - pos < 512) break;
          const block = buf.subarray(pos, pos + 512);
          pos += 512;
          const cont = handleHeader(block);
          if (!cont) { mode = 'done'; break; }
        } else if (mode === 'skip') {
          const avail = buf.length - pos;
          const take = Math.min(remaining + padding, avail);
          // advance past body+padding without copying
          if (take < remaining + padding) {
            const consumedBody = Math.min(take, remaining);
            remaining -= consumedBody;
            padding -= (take - consumedBody);
            pos += take;
            break; // need more data
          } else {
            pos += remaining + padding;
            remaining = 0; padding = 0; mode = 'header';
          }
        } else if (mode === 'collect') {
          const avail = buf.length - pos;
          if (remaining > 0) {
            const take = Math.min(remaining, avail);
            collectBuf.set(buf.subarray(pos, pos + take), collectOff);
            collectOff += take; remaining -= take; pos += take;
            if (remaining > 0) break; // need more
          }
          // consume padding
          const pad = Math.min(padding, buf.length - pos);
          padding -= pad; pos += pad;
          if (padding > 0) break;
          finishCollect();
          mode = 'header';
        } else {
          break; // done
        }
      }

      leftover = pos < buf.length ? buf.subarray(pos) : new Uint8Array(0);
      if (mode === 'done') break;
    }
    try { await reader.cancel(); } catch (e) { /* ignore */ }

    return buildOlxResult(kept);
  }

  // Turn the kept XML map into the same OLXParseResult shape lti-core uses.
  function buildOlxResult(kept) {
    const DOMParserImpl = (typeof DOMParser !== 'undefined')
      ? DOMParser
      : require('@xmldom/xmldom').DOMParser;
    const parse = (s) => new DOMParserImpl().parseFromString(s, 'text/xml');

    const result = { org: '', course: '', run: '', courseId: '', courseTitle: '',
                     blocks: new Map(), errors: [] };

    // Resolve the course root from the shallowest *course.xml, then strip
    // that prefix from every kept path so keys are relative to the root.
    let rootPrefix = null;
    for (const k of kept.keys()) {
      const norm = k.replace(/^\.\//, '');
      if (norm === 'course.xml' || norm.endsWith('/course.xml')) {
        const prefix = norm.slice(0, norm.length - 'course.xml'.length);
        if (rootPrefix === null || prefix.length < rootPrefix.length) rootPrefix = prefix;
      }
    }
    if (rootPrefix === null) { result.errors.push('No course.xml found in the archive.'); return result; }

    const norm = new Map();
    for (const [k, v] of kept) {
      const nk = k.replace(/^\.\//, '');
      norm.set(nk.startsWith(rootPrefix) ? nk.slice(rootPrefix.length) : nk, v);
    }

    const courseXml = norm.get('course.xml');
    if (!courseXml) { result.errors.push('No course.xml found in the archive.'); return result; }
    try {
      const root = parse(courseXml).documentElement;
      result.org = root.getAttribute('org') || '';
      result.course = root.getAttribute('course') || '';
      result.run = root.getAttribute('url_name') || '';
      result.courseTitle = root.getAttribute('display_name') || '';
      const detail = norm.get(`course/${result.run}.xml`);
      if (detail) {
        try { const dn = parse(detail).documentElement.getAttribute('display_name'); if (dn) result.courseTitle = dn; }
        catch (e) { /* ignore */ }
      }
      if (result.org && result.course && result.run) {
        result.courseId = `course-v1:${result.org}+${result.course}+${result.run}`;
      }
    } catch (e) { result.errors.push('Failed to parse course.xml: ' + e.message); return result; }

    const levels = { chapter: new Map(), sequential: new Map(), vertical: new Map() };
    const childElements = (el) => {
      const out = [];
      for (let i = 0; i < el.childNodes.length; i++) if (el.childNodes[i].nodeType === 1) out.push(el.childNodes[i]);
      return out;
    };

    for (const [name, content] of norm) {
      const parts = name.split('/');
      if (parts.length !== 2) continue;
      const dir = parts[0], stem = parts[1].replace(/\.xml$/, '');
      let displayName = '', children = [];
      try {
        const root = parse(content).documentElement;
        displayName = root.getAttribute('display_name') || '';
        if (levels[dir]) {
          for (const c of childElements(root)) {
            const u = c.getAttribute && c.getAttribute('url_name');
            if (u) children.push(u);
          }
        }
      } catch (e) { /* ignore */ }
      result.blocks.set(stem, { blockId: stem, blockType: dir, displayName, chapter: '', sequential: '', vertical: '' });
      if (levels[dir]) levels[dir].set(stem, { displayName, children });
    }

    for (const [, ch] of levels.chapter) {
      for (const seqId of ch.children) {
        const seq = levels.sequential.get(seqId) || { displayName: seqId, children: [] };
        if (result.blocks.has(seqId)) result.blocks.get(seqId).chapter = ch.displayName;
        for (const vertId of seq.children) {
          const vert = levels.vertical.get(vertId) || { displayName: vertId, children: [] };
          if (result.blocks.has(vertId)) {
            result.blocks.get(vertId).chapter = ch.displayName;
            result.blocks.get(vertId).sequential = seq.displayName;
          }
          for (const compId of vert.children) {
            if (result.blocks.has(compId)) {
              const b = result.blocks.get(compId);
              b.chapter = ch.displayName; b.sequential = seq.displayName; b.vertical = vert.displayName;
            }
          }
        }
      }
    }
    return result;
  }

  // ------------------------------------------------------------------
  // Canvas: lightweight pass to extract LTI links (XML entries only).
  // zip.js reads with random access, so media entries are never touched.
  // ------------------------------------------------------------------
  async function readCanvasLinks(zipReader) {
    const DOMParserImpl = (typeof DOMParser !== 'undefined')
      ? DOMParser : require('@xmldom/xmldom').DOMParser;
    const entries = await zipReader.getEntries();
    const byName = new Map();
    for (const e of entries) byName.set(e.filename, e);

    const readText = async (name) => {
      const e = byName.get(name);
      if (!e) return null;
      return e.getData(new zip.TextWriter());
    };

    // Reuse the non-streaming parser by feeding it a JSZip-like shim that
    // only fetches XML on demand.
    const zipShim = {
      file(name) {
        const e = byName.get(name);
        if (!e) return null;
        return { async: async (kind) => e.getData(new zip.TextWriter()) };
      },
    };
    return LTICore.parseCanvas(zipShim, DOMParserImpl);
  }

  // ------------------------------------------------------------------
  // Canvas: streaming rewrite pass. Copies every entry to the output
  // ZipWriter; XML entries are rewritten, all others stream-copied.
  // ------------------------------------------------------------------
  async function streamRewriteCanvas(zipReader, zipWriter, mapping) {
    const entries = await zipReader.getEntries();

    const oldId = mapping.oldCourseId, newId = mapping.newCourseId;
    const oldBlk = oldId ? oldId.replace('course-v1:', 'block-v1:') : '';
    const newBlk = newId ? newId.replace('course-v1:', 'block-v1:') : '';
    const sweep = oldId && newId && oldId !== newId;

    const fileUrlMap = new Map();
    const allPairs = [];
    for (const m of mapping.mappedLinks) {
      if (m.status !== 'MATCHED' || !m.newUrl || m.oldUrl === m.newUrl) continue;
      allPairs.push([m.oldUrl, m.newUrl]);
      if (m.xmlFile) {
        if (!fileUrlMap.has(m.xmlFile)) fileUrlMap.set(m.xmlFile, []);
        fileUrlMap.get(m.xmlFile).push([m.oldUrl, m.newUrl]);
      }
    }

    let updateCount = 0;
    for (const entry of entries) {
      if (entry.directory) {
        await zipWriter.add(entry.filename, null, { directory: true });
        continue;
      }
      if (entry.filename.endsWith('.xml')) {
        // small: buffer, rewrite, write
        let content = await entry.getData(new zip.TextWriter());
        const before = content;
        for (const [o, n] of fileUrlMap.get(entry.filename) || []) content = content.split(o).join(n);
        if (entry.filename === 'course_settings/module_meta.xml') {
          for (const [o, n] of allPairs) content = content.split(o).join(n);
        }
        if (sweep && (content.includes(oldId) || content.includes(oldBlk))) {
          content = content.split(oldId).join(newId).split(oldBlk).join(newBlk);
        }
        if (content !== before) updateCount++;
        await zipWriter.add(entry.filename, new zip.TextReader(content));
      } else {
        // large/media: stream entry -> writer without full buffering.
        // Pass the ReadableStream side directly as the zip.js reader; pump the
        // decompressed entry into the writable side concurrently.
        const ts = new TransformStream();
        const writePromise = zipWriter.add(entry.filename, ts.readable);
        const pumpPromise = entry.getData(ts.writable);
        await Promise.all([pumpPromise, writePromise]);
      }
    }
    return updateCount;
  }

  return { scanOlxStream, buildOlxResult, readCanvasLinks, streamRewriteCanvas, olxPathNeeded };
});
