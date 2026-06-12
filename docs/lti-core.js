/*
 * Canvas LTI Link Updater — client-side core (PROTOTYPE)
 *
 * A JavaScript port of the Python pipeline (src/parsers, src/processors,
 * src/generators) that runs entirely in the browser. No server: files are
 * read, parsed, rewritten, and repackaged in memory.
 *
 * Environment-agnostic: pass in the JSZip constructor, the pako module, and
 * a DOMParser implementation. Works in the browser (native DOMParser) and in
 * Node (via @xmldom/xmldom) so the same code can be unit-tested headlessly.
 */

(function (root, factory) {
  if (typeof module === 'object' && module.exports) {
    module.exports = factory();
  } else {
    root.LTICore = factory();
  }
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  const td = new TextDecoder('utf-8');
  const te = new TextEncoder();

  // ------------------------------------------------------------------
  // XML helpers (namespace-agnostic, mirrors the Python localName logic)
  // ------------------------------------------------------------------
  function parseXml(str, DOMParserImpl) {
    return new DOMParserImpl().parseFromString(str, 'text/xml');
  }

  function elementsByLocal(node, localName) {
    const out = [];
    const all = node.getElementsByTagName('*');
    for (let i = 0; i < all.length; i++) {
      const el = all[i];
      const local = el.localName || el.nodeName.split(':').pop();
      if (local === localName) out.push(el);
    }
    return out;
  }

  function childrenByLocal(el, localName) {
    const out = [];
    for (let i = 0; i < el.childNodes.length; i++) {
      const c = el.childNodes[i];
      if (c.nodeType === 1) {
        const local = c.localName || c.nodeName.split(':').pop();
        if (local === localName) out.push(c);
      }
    }
    return out;
  }

  function childElements(el) {
    const out = [];
    for (let i = 0; i < el.childNodes.length; i++) {
      if (el.childNodes[i].nodeType === 1) out.push(el.childNodes[i]);
    }
    return out;
  }

  function text(el) {
    return el && el.textContent ? el.textContent.trim() : '';
  }

  // ------------------------------------------------------------------
  // tar.gz reader (handles ustar prefix, GNU long names, pax headers)
  // ------------------------------------------------------------------
  function untarGz(bytes, pako) {
    const tar = pako.ungzip(bytes);
    const entries = new Map(); // path -> Uint8Array
    let off = 0;
    let paxPath = null;
    let gnuLongName = null;

    const str = (start, len) => {
      let end = start;
      const max = start + len;
      while (end < max && tar[end] !== 0) end++;
      return td.decode(tar.subarray(start, end));
    };

    while (off + 512 <= tar.length) {
      // End: block of zeros
      let allZero = true;
      for (let i = off; i < off + 512; i++) {
        if (tar[i] !== 0) { allZero = false; break; }
      }
      if (allZero) break;

      let name = str(off, 100);
      const sizeStr = str(off + 124, 12).trim();
      const size = parseInt(sizeStr || '0', 8) || 0;
      const type = String.fromCharCode(tar[off + 156] || 48);
      const prefix = str(off + 345, 155);
      if (prefix) name = prefix + '/' + name;

      const dataStart = off + 512;
      const data = tar.subarray(dataStart, dataStart + size);

      if (type === 'x' || type === 'g') {
        // pax extended header: "len key=value\n" records
        if (type === 'x') {
          const s = td.decode(data);
          let p = 0;
          while (p < s.length) {
            const sp = s.indexOf(' ', p);
            if (sp < 0) break;
            const len = parseInt(s.slice(p, sp), 10);
            if (!len) break;
            const rec = s.slice(sp + 1, p + len - 1); // strip trailing \n
            const eq = rec.indexOf('=');
            if (eq > 0 && rec.slice(0, eq) === 'path') paxPath = rec.slice(eq + 1);
            p += len;
          }
        }
      } else if (type === 'L') {
        gnuLongName = td.decode(data).replace(/\0+$/, '');
      } else if (type === '0' || type === '\0' || type === '7') {
        const finalName = paxPath || gnuLongName || name;
        entries.set(finalName.replace(/^\.\//, ''), new Uint8Array(data));
        paxPath = null;
        gnuLongName = null;
      } else {
        // directory ('5'), links, etc. — track dirs implicitly; reset overrides
        paxPath = null;
        gnuLongName = null;
      }

      off = dataStart + Math.ceil(size / 512) * 512;
    }
    return entries;
  }

  // ------------------------------------------------------------------
  // edX URL parsing (mirrors CanvasLTIParser._parse_edx_url)
  // ------------------------------------------------------------------
  const COURSE_RE = /course-v1:([^/+]+)\+([^/+]+)\+([^/+\s?#]+)/;
  const BLOCK_RE = /block-v1:([^/+]+)\+([^/+]+)\+([^/+]+)\+type@([^/+]+)\+block@([^/+\s?#&]+)/;

  function parseEdxUrl(link) {
    const url = link.launchUrl;
    const cm = url.match(COURSE_RE);
    if (cm) {
      link.edxOrg = cm[1]; link.edxCourse = cm[2]; link.edxRun = cm[3];
      link.rawCourseId = `course-v1:${cm[1]}+${cm[2]}+${cm[3]}`;
    }
    const bm = url.match(BLOCK_RE);
    if (bm) {
      link.edxOrg = bm[1]; link.edxCourse = bm[2]; link.edxRun = bm[3];
      link.edxBlockType = bm[4]; link.edxBlockId = bm[5];
      link.rawCourseId = `course-v1:${bm[1]}+${bm[2]}+${bm[3]}`;
    }
    // Query-string usage keys
    try {
      const qs = url.split('?')[1];
      if (qs) {
        for (const pair of qs.split('&')) {
          const [k, v] = pair.split('=');
          if (['usage_key', 'id', 'block_id'].includes(k) && v) {
            const dv = decodeURIComponent(v);
            const qbm = dv.match(/block-v1:([^+]+)\+([^+]+)\+([^+]+)\+type@([^+]+)\+block@(.+)/);
            if (qbm) {
              link.edxOrg = qbm[1]; link.edxCourse = qbm[2]; link.edxRun = qbm[3];
              link.edxBlockType = qbm[4]; link.edxBlockId = qbm[5];
            }
          }
        }
      }
    } catch (e) { /* ignore malformed query strings */ }
  }

  // ------------------------------------------------------------------
  // Canvas IMSCC parsing (mirrors CanvasLTIParser)
  // ------------------------------------------------------------------
  async function parseCanvas(zip, DOMParserImpl) {
    const result = { courseTitle: 'Unknown Course', ltiLinks: [], errors: [] };

    const manifestFile = zip.file('imsmanifest.xml');
    if (!manifestFile) {
      result.errors.push('No imsmanifest.xml found in the .imscc archive.');
      return result;
    }
    const manifest = parseXml(await manifestFile.async('string'), DOMParserImpl);

    // Course title: LOM <title><string> then any <title>
    for (const s of elementsByLocal(manifest, 'string')) {
      const parent = s.parentNode;
      const plocal = parent && (parent.localName || parent.nodeName.split(':').pop());
      if (plocal === 'title' && text(s)) { result.courseTitle = text(s); break; }
    }
    if (result.courseTitle === 'Unknown Course') {
      for (const t of elementsByLocal(manifest, 'title')) {
        if (text(t)) { result.courseTitle = text(t); break; }
      }
    }

    const readXml = async (path) => {
      const f = zip.file(path);
      if (!f) return null;
      try { return parseXml(await f.async('string'), DOMParserImpl); }
      catch (e) { return null; }
    };

    const resources = elementsByLocal(manifest, 'resource');
    const existingIds = new Set();

    for (const res of resources) {
      const resType = (res.getAttribute('type') || '').toLowerCase();
      const resId = res.getAttribute('identifier') || '';

      if (resType.includes('basiclti')) {
        // Find the XML file for this LTI resource
        let xmlFile = null;
        for (const f of childrenByLocal(res, 'file')) {
          const href = f.getAttribute('href') || '';
          if (href.endsWith('.xml')) { xmlFile = href; break; }
        }
        if (!xmlFile) {
          const href = res.getAttribute('href') || '';
          if (href.endsWith('.xml')) xmlFile = href;
        }
        if (!xmlFile) {
          for (const pattern of [`${resId}/basiclti_link.xml`, `${resId}/basic_lti_link.xml`, `${resId}.xml`]) {
            if (zip.file(pattern)) { xmlFile = pattern; break; }
          }
        }
        if (!xmlFile) continue;

        const doc = await readXml(xmlFile);
        if (!doc) continue;

        let launchUrl = '';
        for (const tag of ['launch_url', 'secure_launch_url']) {
          const els = elementsByLocal(doc, tag);
          if (els.length && text(els[0])) { launchUrl = text(els[0]); break; }
        }
        if (!launchUrl) {
          // extension properties (named url fields first)
          for (const prop of elementsByLocal(doc, 'property')) {
            const name = prop.getAttribute('name') || '';
            if (['url', 'launch_url', 'tool_id'].includes(name) && text(prop).startsWith('http')) {
              launchUrl = text(prop); break;
            }
          }
        }
        if (!launchUrl) {
          // loose fallback (mirrors Python): any property/custom value that is
          // an http URL mentioning edx/mitx/lti — catches tool-config links
          // like <lticm:property name="domain">https://lms.mitx.mit.edu</...>
          const all = doc.getElementsByTagName('*');
          for (let i = 0; i < all.length && !launchUrl; i++) {
            const el = all[i];
            const local = (el.localName || el.nodeName.split(':').pop()).toLowerCase();
            if (local.includes('custom') || local.includes('property')) {
              const t = text(el);
              if (t.includes('http') &&
                  (t.includes('edx.org') || t.toLowerCase().includes('mitx') ||
                   t.toLowerCase().includes('lti'))) {
                launchUrl = t;
              }
            }
          }
        }
        if (!launchUrl) continue;

        let title = '';
        const titleEls = elementsByLocal(doc, 'title');
        if (titleEls.length) title = text(titleEls[0]);

        const link = {
          resourceId: resId, title, launchUrl, xmlFile,
          edxOrg: '', edxCourse: '', edxRun: '', edxBlockType: '', edxBlockId: '', rawCourseId: '',
        };
        parseEdxUrl(link);
        result.ltiLinks.push(link);
        existingIds.add(resId);

      } else if (resType.includes('assignment') || resType.includes('associatedcontent')) {
        for (const f of childrenByLocal(res, 'file')) {
          const href = f.getAttribute('href') || '';
          if (!href) continue;
          const doc = await readXml(href);
          if (!doc) continue;

          let found = null;
          const all = doc.getElementsByTagName('*');
          for (let i = 0; i < all.length; i++) {
            const el = all[i];
            const local = el.localName || el.nodeName.split(':').pop();
            if (['external_tool_url', 'url', 'external_tool_tag_attributes'].includes(local)) {
              const t = text(el);
              if (t && (t.includes('edx.org') || t.toLowerCase().includes('lti'))) {
                found = t;
                break;
              }
            }
          }
          if (found) {
            let title = resId;
            const titleEls = elementsByLocal(doc, 'title');
            if (titleEls.length && text(titleEls[0])) title = text(titleEls[0]);
            const link = {
              resourceId: resId, title, launchUrl: found, xmlFile: href,
              edxOrg: '', edxCourse: '', edxRun: '', edxBlockType: '', edxBlockId: '', rawCourseId: '',
            };
            parseEdxUrl(link);
            result.ltiLinks.push(link);
            existingIds.add(resId);
            break;
          }
        }
      }
    }

    // ContextExternalTool items in module_meta.xml not already captured
    const metaDoc = await readXml('course_settings/module_meta.xml');
    if (metaDoc) {
      for (const mod of elementsByLocal(metaDoc, 'module')) {
        const items = elementsByLocal(mod, 'item');
        for (let pos = 0; pos < items.length; pos++) {
          const item = items[pos];
          const ct = childrenByLocal(item, 'content_type');
          if (!ct.length || text(ct[0]) !== 'ContextExternalTool') continue;
          const irefEls = childrenByLocal(item, 'identifierref');
          const iref = irefEls.length ? text(irefEls[0]) : '';
          if (iref && existingIds.has(iref)) continue;
          const titleEls = childrenByLocal(item, 'title');
          const urlEls = childrenByLocal(item, 'url');
          const url = urlEls.length ? text(urlEls[0]) : '';
          if (url && (url.includes('edx.org') || url.toLowerCase().includes('edx') || url.toLowerCase().includes('lti'))) {
            const link = {
              resourceId: iref || `module_item_${pos}`,
              title: titleEls.length ? text(titleEls[0]) : '',
              launchUrl: url, xmlFile: '',
              edxOrg: '', edxCourse: '', edxRun: '', edxBlockType: '', edxBlockId: '', rawCourseId: '',
            };
            parseEdxUrl(link);
            result.ltiLinks.push(link);
            existingIds.add(link.resourceId);
          }
        }
      }
    }

    return result;
  }

  // ------------------------------------------------------------------
  // edX OLX parsing (mirrors OLXParser)
  // ------------------------------------------------------------------
  const SKIP_DIRS = new Set(['course', 'policies', 'static', 'tabs', 'drafts',
                             'about', 'info', 'assets', '__MACOSX']);

  function isJunk(path) {
    const base = path.split('/').pop();
    return base.startsWith('._') || path.includes('__MACOSX');
  }

  function parseOLX(entries, DOMParserImpl) {
    const result = {
      org: '', course: '', run: '', courseId: '', courseTitle: '',
      blocks: new Map(), errors: [],
    };

    // Find course.xml (shallowest)
    let rootPrefix = null;
    let best = null;
    for (const path of entries.keys()) {
      if (isJunk(path)) continue;
      if (path === 'course.xml' || path.endsWith('/course.xml')) {
        const depth = path.split('/').length;
        if (best === null || depth < best.depth) best = { path, depth };
      }
    }
    if (!best) {
      result.errors.push('No course.xml found in the archive.');
      return result;
    }
    rootPrefix = best.path.slice(0, best.path.length - 'course.xml'.length); // '' or 'course/'

    const readEntry = (rel) => {
      const data = entries.get(rootPrefix + rel);
      return data ? td.decode(data) : null;
    };

    const courseXml = readEntry('course.xml');
    try {
      const doc = parseXml(courseXml, DOMParserImpl);
      const rootEl = doc.documentElement;
      result.org = rootEl.getAttribute('org') || '';
      result.course = rootEl.getAttribute('course') || '';
      result.run = rootEl.getAttribute('url_name') || '';
      result.courseTitle = rootEl.getAttribute('display_name') || '';
      if (result.run) {
        const detail = readEntry(`course/${result.run}.xml`);
        if (detail) {
          try {
            const ddoc = parseXml(detail, DOMParserImpl);
            const dn = ddoc.documentElement.getAttribute('display_name');
            if (dn) result.courseTitle = dn;
          } catch (e) { /* ignore */ }
        }
      }
      if (result.org && result.course && result.run) {
        result.courseId = `course-v1:${result.org}+${result.course}+${result.run}`;
      }
    } catch (e) {
      result.errors.push('Failed to parse course.xml: ' + e.message);
      return result;
    }

    // Collect files by top-level directory under the course root
    const byDir = new Map(); // dir -> [{stem, path}]
    for (const path of entries.keys()) {
      if (!path.startsWith(rootPrefix) || isJunk(path)) continue;
      const rel = path.slice(rootPrefix.length);
      const parts = rel.split('/');
      if (parts.length !== 2 || !parts[1].endsWith('.xml')) continue;
      const dir = parts[0];
      if (SKIP_DIRS.has(dir) || dir.startsWith('.')) continue;
      if (!byDir.has(dir)) byDir.set(dir, []);
      byDir.get(dir).push({ stem: parts[1].slice(0, -4), path });
    }

    // Hierarchy maps for chapter/sequential/vertical
    const levels = {};
    for (const level of ['chapter', 'sequential', 'vertical']) {
      const items = new Map();
      for (const { stem, path } of byDir.get(level) || []) {
        let displayName = '';
        const children = [];
        try {
          const doc = parseXml(td.decode(entries.get(path)), DOMParserImpl);
          displayName = doc.documentElement.getAttribute('display_name') || '';
          for (const child of childElements(doc.documentElement)) {
            const urlName = child.getAttribute && child.getAttribute('url_name');
            if (urlName) children.push(urlName);
          }
        } catch (e) { /* ignore */ }
        items.set(stem, { displayName, children });
      }
      levels[level] = items;
    }

    // Block inventory: every dir with XML files (incl. chapter/sequential/vertical)
    for (const [dir, files] of byDir) {
      for (const { stem, path } of files) {
        let displayName = '';
        try {
          const doc = parseXml(td.decode(entries.get(path)), DOMParserImpl);
          displayName = doc.documentElement.getAttribute('display_name') || '';
        } catch (e) { /* ignore */ }
        result.blocks.set(stem, {
          blockId: stem, blockType: dir, displayName,
          chapter: '', sequential: '', vertical: '',
        });
      }
    }

    // Assign hierarchy context
    for (const [chId, ch] of levels.chapter) {
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
              b.chapter = ch.displayName;
              b.sequential = seq.displayName;
              b.vertical = vert.displayName;
            }
          }
        }
      }
    }

    return result;
  }

  // ------------------------------------------------------------------
  // Mapping (mirrors LTIMapper)
  // ------------------------------------------------------------------
  function buildLocation(block) {
    return [block.chapter, block.sequential, block.vertical].filter(Boolean).join(' > ');
  }

  function isCourseLevelUrl(url) {
    if (!url) return false;
    const hasCourse = /course-v1:/.test(url);
    const hasBlock = /block-v1:|block@/.test(url);
    const isCourseware = /\/courseware\/?$|\/course\/?$|\/about\/?$/.test(url);
    return hasCourse && (!hasBlock || isCourseware);
  }

  function mapLinks(ltiLinks, olx) {
    const result = {
      mappedLinks: [], matched: 0, missing: 0, newOnly: 0,
      total: ltiLinks.length, oldCourseId: '', newCourseId: olx.courseId,
      warnings: [],
    };

    for (const l of ltiLinks) {
      if (l.rawCourseId) { result.oldCourseId = l.rawCourseId; break; }
    }

    const newTriple = `${olx.org}+${olx.course}+${olx.run}`;
    const referenced = new Set();

    for (const link of ltiLinks) {
      if (!link.edxBlockId) {
        const courseLevel = isCourseLevelUrl(link.launchUrl);
        result.mappedLinks.push({
          status: courseLevel ? 'MATCHED' : 'MISSING',
          resourceTitle: link.title,
          oldUrl: link.launchUrl,
          newUrl: courseLevel
            ? link.launchUrl.replace(COURSE_RE, `course-v1:${newTriple}`)
            : '',
          blockId: '', blockType: '', location: '',
          notes: courseLevel ? 'Course-level URL (no specific block)'
                             : 'Could not parse block ID from URL',
          resourceId: link.resourceId, xmlFile: link.xmlFile,
        });
        if (courseLevel) result.matched++; else result.missing++;
        continue;
      }

      const block = olx.blocks.get(link.edxBlockId);
      if (block) {
        const oldTriple = `${link.edxOrg}+${link.edxCourse}+${link.edxRun}`;
        const newUrl = link.launchUrl.split(`course-v1:${oldTriple}`).join(`course-v1:${newTriple}`)
                                     .split(`block-v1:${oldTriple}`).join(`block-v1:${newTriple}`);
        result.mappedLinks.push({
          status: 'MATCHED',
          resourceTitle: link.title || block.displayName,
          oldUrl: link.launchUrl, newUrl,
          blockId: link.edxBlockId,
          blockType: block.blockType || link.edxBlockType,
          location: buildLocation(block),
          notes: '',
          resourceId: link.resourceId, xmlFile: link.xmlFile,
        });
        result.matched++;
        referenced.add(link.edxBlockId);
      } else {
        result.mappedLinks.push({
          status: 'MISSING',
          resourceTitle: link.title,
          oldUrl: link.launchUrl, newUrl: '',
          blockId: link.edxBlockId, blockType: link.edxBlockType,
          location: '', notes: 'Block ID not found in new edX course',
          resourceId: link.resourceId, xmlFile: link.xmlFile,
        });
        result.missing++;
      }
    }

    // NEW_ONLY: linkable blocks (sequential/vertical) with no Canvas link
    for (const [blockId, block] of olx.blocks) {
      if (!['sequential', 'vertical'].includes(block.blockType)) continue;
      if (referenced.has(blockId)) continue;
      result.mappedLinks.push({
        status: 'NEW_ONLY',
        resourceTitle: block.displayName || blockId,
        oldUrl: '', newUrl: '',
        blockId, blockType: block.blockType,
        location: buildLocation(block),
        notes: 'Block exists in new edX course but has no Canvas LTI link',
        resourceId: '', xmlFile: '',
      });
      result.newOnly++;
    }

    // Warnings (mirrors LTIMapper.map)
    if (result.oldCourseId && result.oldCourseId === result.newCourseId) {
      result.warnings.push(
        'The Canvas links already point to this exact edX course run, so the ' +
        'output will be identical to the input. If you meant to move links to ' +
        'a NEW run, check that you exported the OLD Canvas course and the NEW ' +
        'edX course.');
    }
    const linkOrgs = new Set(ltiLinks.map(l => l.edxOrg).filter(Boolean));
    if (linkOrgs.size && olx.org && !linkOrgs.has(olx.org)) {
      result.warnings.push(
        `None of the Canvas LTI links reference the organization "${olx.org}" ` +
        `found in the edX export (links reference: ${[...linkOrgs].sort().join(', ')}). ` +
        'This usually means the wrong edX course was exported.');
    }
    if (result.missing > 0) {
      result.warnings.push(
        `${result.missing} LTI link(s) reference blocks not found in the new ` +
        'edX course. These have been excluded from the updated export.');
    }
    if (!result.oldCourseId) {
      result.warnings.push(
        'Could not determine the old edX course ID from the LTI URLs. ' +
        'URL replacement may not work correctly.');
    }

    return result;
  }

  // ------------------------------------------------------------------
  // Updated IMSCC generation (mirrors IMSCCUpdater)
  // ------------------------------------------------------------------
  async function generateUpdatedImscc(zip, mapping, JSZipImpl) {
    const out = new JSZipImpl();
    const oldId = mapping.oldCourseId;
    const newId = mapping.newCourseId;
    const oldBlockPrefix = oldId ? oldId.replace('course-v1:', 'block-v1:') : '';
    const newBlockPrefix = newId ? newId.replace('course-v1:', 'block-v1:') : '';
    const sweep = oldId && newId && oldId !== newId;

    // Per-file URL replacements for MATCHED links
    const fileUrlMap = new Map(); // xmlFile path -> [{oldUrl, newUrl}]
    const allUrlPairs = [];
    for (const m of mapping.mappedLinks) {
      if (m.status !== 'MATCHED' || !m.newUrl || m.oldUrl === m.newUrl) continue;
      allUrlPairs.push([m.oldUrl, m.newUrl]);
      if (m.xmlFile) {
        if (!fileUrlMap.has(m.xmlFile)) fileUrlMap.set(m.xmlFile, []);
        fileUrlMap.get(m.xmlFile).push({ oldUrl: m.oldUrl, newUrl: m.newUrl });
      }
    }

    let updateCount = 0;
    const names = Object.keys(zip.files);
    for (const name of names) {
      const entry = zip.files[name];
      if (entry.dir) {
        out.folder(name.replace(/\/$/, ''));
        continue;
      }

      if (name.endsWith('.xml')) {
        let content = await entry.async('string');
        const before = content;

        for (const { oldUrl, newUrl } of fileUrlMap.get(name) || []) {
          content = content.split(oldUrl).join(newUrl);
        }
        if (name === 'course_settings/module_meta.xml') {
          for (const [oldUrl, newUrl] of allUrlPairs) {
            content = content.split(oldUrl).join(newUrl);
          }
        }
        if (sweep && (content.includes(oldId) || content.includes(oldBlockPrefix))) {
          content = content.split(oldId).join(newId);
          content = content.split(oldBlockPrefix).join(newBlockPrefix);
        }

        if (content !== before) updateCount++;
        out.file(name, content);
      } else {
        out.file(name, await entry.async('uint8array'));
      }
    }

    const bytes = await out.generateAsync({
      type: 'uint8array',
      compression: 'DEFLATE',
      compressionOptions: { level: 6 },
    });
    return { bytes, updateCount };
  }

  // ------------------------------------------------------------------
  // Audit CSV (mirrors generate_audit_csv)
  // ------------------------------------------------------------------
  const STATUS_ORDER = { MISSING: 0, NEW_ONLY: 1, MATCHED: 2 };

  function csvField(v) {
    v = v == null ? '' : String(v);
    if (/[",\r\n]/.test(v)) return '"' + v.replace(/"/g, '""') + '"';
    return v;
  }

  function generateAuditCsv(mapping) {
    const rows = [...mapping.mappedLinks].sort((a, b) => {
      const so = (STATUS_ORDER[a.status] ?? 99) - (STATUS_ORDER[b.status] ?? 99);
      if (so) return so;
      return a.resourceTitle.toLowerCase() < b.resourceTitle.toLowerCase() ? -1
           : a.resourceTitle.toLowerCase() > b.resourceTitle.toLowerCase() ? 1 : 0;
    });
    const lines = ['status,resource_title,old_lti_url,new_lti_url,block_id,block_type,edx_location,notes'];
    for (const r of rows) {
      lines.push([r.status, r.resourceTitle, r.oldUrl, r.newUrl, r.blockId,
                  r.blockType, r.location, r.notes].map(csvField).join(','));
    }
    return lines.join('\r\n') + '\r\n';
  }

  // ------------------------------------------------------------------
  // File detection (mirrors src/file_detect.py)
  // ------------------------------------------------------------------
  async function identifyFile(bytes, deps) {
    // Canvas: ZIP containing imsmanifest.xml
    try {
      const zip = await deps.JSZip.loadAsync(bytes);
      const hasManifest = Object.keys(zip.files).some(
        n => n.split('/').pop() === 'imsmanifest.xml');
      if (hasManifest) return { kind: 'canvas', zip };
    } catch (e) { /* not a zip */ }
    // edX: gzipped tar containing course.xml
    try {
      const entries = untarGz(bytes, deps.pako);
      for (const path of entries.keys()) {
        if (path.split('/').pop() === 'course.xml') return { kind: 'edx', entries };
      }
    } catch (e) { /* not a tar.gz */ }
    return { kind: 'unknown' };
  }

  // ------------------------------------------------------------------
  // Full pipeline
  // ------------------------------------------------------------------
  async function runPipeline(canvasZip, olxEntries, deps) {
    const canvas = await parseCanvas(canvasZip, deps.DOMParser);
    if (canvas.errors.length) throw new Error(canvas.errors.join('; '));
    if (!canvas.ltiLinks.length) throw new Error('No LTI links found in the Canvas export.');

    const olx = parseOLX(olxEntries, deps.DOMParser);
    if (olx.errors.length) throw new Error(olx.errors.join('; '));
    if (!olx.blocks.size) throw new Error('No blocks found in the edX export.');

    const mapping = mapLinks(canvas.ltiLinks, olx);
    const { bytes, updateCount } = await generateUpdatedImscc(canvasZip, mapping, deps.JSZip);
    const csv = generateAuditCsv(mapping);

    return { canvas, olx, mapping, imsccBytes: bytes, updateCount, csv };
  }

  return {
    untarGz, parseCanvas, parseOLX, mapLinks,
    generateUpdatedImscc, generateAuditCsv, identifyFile, runPipeline,
  };
});
