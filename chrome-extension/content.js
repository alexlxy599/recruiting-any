// Talent Pool - LinkedIn Profile Sync
// Extracts structured data from LinkedIn profile pages and syncs to local talent pool

(function () {
  'use strict';

  const API_BASE = 'http://127.0.0.1:5055';
  const DEBOUNCE_MS = 2500;

  let lastSyncedUrl = '';
  let syncTimer = null;

  // ── Main ──

  function init() {
    createSideTab();
    waitForProfile().then(() => syncProfile());

    // Watch for SPA navigation
    let currentPath = window.location.pathname;
    setInterval(() => {
      if (window.location.pathname !== currentPath) {
        currentPath = window.location.pathname;
        if (currentPath.startsWith('/in/')) {
          clearTimeout(syncTimer);
          syncTimer = setTimeout(() => {
            waitForProfile().then(() => syncProfile());
          }, DEBOUNCE_MS);
        }
      }
    }, 1000);
  }

  function findNameElement() {
    // Try multiple selectors - LinkedIn changes class names frequently
    const selectors = [
      'h1.text-heading-xlarge',
      '.pv-top-card h1',
      '.ph5 h1',
      '.mt2 h1',
      'h1.inline.t-24',
      'h1.text-heading-xlarge.inline',
      '[data-anonymize="person-name"]',
      '.pv-text-details__left-panel h1',
      'main section h1',
      'main h1',
      'h1',
    ];
    // Debug: log all h1s on the page
    const allH1s = document.querySelectorAll('h1');
    console.log('[TalentPool] All h1 elements on page:', allH1s.length, Array.from(allH1s).map(el => `"${el.textContent.trim().slice(0, 50)}" class="${el.className}"`));

    for (const sel of selectors) {
      const el = document.querySelector(sel);
      if (el && el.textContent.trim().length > 1 && el.textContent.trim().length < 60) {
        return el;
      }
    }
    return null;
  }

  // Fallback: extract name from page title ("FirstName LastName - headline | LinkedIn")
  function getNameFromTitle() {
    const title = document.title || '';
    // LinkedIn title format: "Name - Title | LinkedIn" or "Name | LinkedIn"
    const match = title.match(/^(.+?)\s*[-–|]/) || title.match(/^(.+?)\s*\|/);
    if (match) {
      const name = match[1].trim();
      if (name.length > 1 && name.length < 50 && name !== 'LinkedIn') {
        return name;
      }
    }
    return '';
  }

  function waitForProfile() {
    return new Promise((resolve) => {
      let attempts = 0;
      const check = () => {
        attempts++;
        const nameEl = findNameElement();
        if (nameEl || attempts > 40) {
          // Extra wait for content to fully load
          setTimeout(resolve, 1500);
        } else {
          setTimeout(check, 500);
        }
      };
      check();
    });
  }

  // ── Side Tab UI ──

  function createSideTab() {
    const tab = document.createElement('div');
    tab.id = 'tp-side-tab';
    tab.innerHTML = `
      <div class="tp-tab-collapsed" id="tp-tab-trigger">
        <span class="tp-tab-icon">TP</span>
      </div>
      <div class="tp-tab-panel" id="tp-tab-panel">
        <div class="tp-panel-header">
          <span class="tp-panel-title">Talent Pool</span>
          <button class="tp-panel-close" id="tp-panel-close">&times;</button>
        </div>
        <div class="tp-panel-body" id="tp-panel-body">
          <div class="tp-status-row">
            <span class="tp-status-dot tp-dot-waiting"></span>
            <span class="tp-status-text">等待同步...</span>
          </div>
        </div>
        <div class="tp-panel-footer" id="tp-panel-footer" style="display:none;">
          <a id="tp-view-link" href="#" target="_blank" class="tp-view-btn">查看详情</a>
          <button id="tp-resync-btn" class="tp-resync-btn">重新同步</button>
        </div>
      </div>
    `;
    document.body.appendChild(tab);

    // Toggle panel
    document.getElementById('tp-tab-trigger').addEventListener('click', () => {
      tab.classList.toggle('tp-expanded');
    });
    document.getElementById('tp-panel-close').addEventListener('click', () => {
      tab.classList.remove('tp-expanded');
    });
    document.getElementById('tp-resync-btn').addEventListener('click', () => {
      lastSyncedUrl = '';
      syncProfile();
    });
  }

  function updateSideTab(status, data = {}) {
    const body = document.getElementById('tp-panel-body');
    const footer = document.getElementById('tp-panel-footer');
    const trigger = document.getElementById('tp-tab-trigger');

    if (status === 'syncing') {
      trigger.className = 'tp-tab-collapsed tp-trigger-syncing';
      body.innerHTML = `
        <div class="tp-status-row">
          <span class="tp-status-dot tp-dot-syncing"></span>
          <span class="tp-status-text">同步中...</span>
        </div>
      `;
      footer.style.display = 'none';
    } else if (status === 'created') {
      trigger.className = 'tp-tab-collapsed tp-trigger-success';
      body.innerHTML = `
        <div class="tp-status-row">
          <span class="tp-status-dot tp-dot-success"></span>
          <span class="tp-status-text">已新增到人才库</span>
        </div>
        <div class="tp-person-info">
          <div class="tp-person-name">${esc(data.name || '')}</div>
          <div class="tp-person-detail">${esc(data.headline || '')}</div>
        </div>
      `;
      footer.style.display = 'flex';
      document.getElementById('tp-view-link').href = `${API_BASE}/people/${data.person_id}`;
    } else if (status === 'updated') {
      trigger.className = 'tp-tab-collapsed tp-trigger-success';
      body.innerHTML = `
        <div class="tp-status-row">
          <span class="tp-status-dot tp-dot-updated"></span>
          <span class="tp-status-text">已更新 Profile</span>
        </div>
        <div class="tp-person-info">
          <div class="tp-person-name">${esc(data.name || '')}</div>
          <div class="tp-person-detail">${esc(data.headline || '')}</div>
        </div>
      `;
      footer.style.display = 'flex';
      document.getElementById('tp-view-link').href = `${API_BASE}/people/${data.person_id}`;
    } else if (status === 'skipped') {
      trigger.className = 'tp-tab-collapsed tp-trigger-neutral';
      body.innerHTML = `
        <div class="tp-status-row">
          <span class="tp-status-dot tp-dot-neutral"></span>
          <span class="tp-status-text">已在库中（无变化）</span>
        </div>
        <div class="tp-person-info">
          <div class="tp-person-name">${esc(data.name || '')}</div>
          <div class="tp-person-detail">${esc(data.headline || '')}</div>
        </div>
      `;
      footer.style.display = 'flex';
      document.getElementById('tp-view-link').href = `${API_BASE}/people/${data.person_id}`;
    } else if (status === 'offline') {
      trigger.className = 'tp-tab-collapsed tp-trigger-offline';
      body.innerHTML = `
        <div class="tp-status-row">
          <span class="tp-status-dot tp-dot-offline"></span>
          <span class="tp-status-text">服务未启动</span>
        </div>
        <div class="tp-hint">请确保 Flask 在 127.0.0.1:5055 运行</div>
      `;
      footer.style.display = 'none';
    } else if (status === 'error') {
      trigger.className = 'tp-tab-collapsed tp-trigger-offline';
      body.innerHTML = `
        <div class="tp-status-row">
          <span class="tp-status-dot tp-dot-offline"></span>
          <span class="tp-status-text">提取失败</span>
        </div>
        <div class="tp-hint">${esc(data.message || '无法读取页面内容')}</div>
      `;
      footer.style.display = 'none';
    }
  }

  // ── Extract Data ──

  function extractProfile() {
    const data = {
      linkedin_url: normalizeLinkedInUrl(window.location.href),
      first_name: '',
      last_name: '',
      headline: '',
      location: '',
      about: '',
      experiences: [],
      educations: [],
    };

    // Name
    const nameEl = findNameElement();
    console.log('[TalentPool] Name element:', nameEl, nameEl?.textContent);
    let fullName = '';
    if (nameEl) {
      fullName = nameEl.textContent.trim();
    } else {
      // Fallback: extract from page title
      fullName = getNameFromTitle();
      console.log('[TalentPool] Using title fallback:', fullName);
    }
    if (fullName) {
      const parts = fullName.split(/\s+/);
      data.first_name = parts[0] || '';
      data.last_name = parts.slice(1).join(' ') || '';
    }

    // Headline - try multiple selectors
    const headlineEl = document.querySelector('.text-body-medium.break-words')
      || document.querySelector('.pv-top-card .text-body-medium')
      || document.querySelector('[data-generated-suggestion-target]')
      || document.querySelector('.pv-text-details__left-panel .text-body-medium')
      || document.querySelector('main section .text-body-medium');
    if (headlineEl) {
      data.headline = headlineEl.textContent.trim();
    }

    // Location
    const locationEl = document.querySelector('.text-body-small.inline.t-black--light.break-words')
      || document.querySelector('.pv-top-card .pb2.pv-text-details__left-panel .text-body-small');
    if (locationEl) {
      data.location = locationEl.textContent.trim();
    }

    // About section
    const aboutSections = document.querySelectorAll('section');
    for (const section of aboutSections) {
      const header = section.querySelector('#about');
      if (header) {
        const textEl = section.querySelector('.inline-show-more-text, .pv-shared-text-with-see-more span[aria-hidden="true"]');
        if (textEl) data.about = textEl.textContent.trim().slice(0, 500);
        break;
      }
    }

    // Experience section
    for (const section of document.querySelectorAll('section')) {
      if (section.querySelector('#experience')) {
        const items = section.querySelectorAll(':scope > div > div > div > ul > li.artdeco-list__item');
        items.forEach((item, idx) => {
          const exps = extractExperience(item, idx);
          data.experiences.push(...exps);
        });
        break;
      }
    }

    // Education section
    for (const section of document.querySelectorAll('section')) {
      if (section.querySelector('#education')) {
        const items = section.querySelectorAll(':scope > div > div > div > ul > li.artdeco-list__item');
        items.forEach((item) => {
          const edu = extractEducation(item);
          if (edu) data.educations.push(edu);
        });
        break;
      }
    }

    return data;
  }

  function extractExperience(item, position) {
    const results = [];
    const spans = item.querySelectorAll('span[aria-hidden="true"]');
    const texts = Array.from(spans).map(s => s.textContent.trim()).filter(Boolean);
    if (!texts.length) return results;

    // Check for grouped roles (multiple positions at same company)
    const innerList = item.querySelector(':scope > div > div > div > ul');
    if (innerList) {
      const company = texts[0] || '';
      const subItems = innerList.querySelectorAll(':scope > li');
      subItems.forEach((sub, idx) => {
        const subSpans = sub.querySelectorAll('span[aria-hidden="true"]');
        const subTexts = Array.from(subSpans).map(s => s.textContent.trim()).filter(Boolean);
        if (subTexts.length > 0) {
          results.push({
            title: subTexts[0] || '',
            company: company,
            position: position + idx,
            is_current: (position === 0 && idx === 0) ? 1 : 0,
            start_year: extractYear(subTexts),
            end_year: null,
          });
        }
      });
    } else {
      // Single role
      results.push({
        title: texts[0] || '',
        company: texts[1] || '',
        position: position,
        is_current: position === 0 ? 1 : 0,
        start_year: extractYear(texts),
        end_year: null,
      });
    }

    return results;
  }

  function extractEducation(item) {
    const spans = item.querySelectorAll('span[aria-hidden="true"]');
    const texts = Array.from(spans).map(s => s.textContent.trim()).filter(Boolean);
    if (!texts.length) return null;

    return {
      school: texts[0] || '',
      degree: texts[1] || '',
      field: texts[2] || '',
      start_year: extractYear(texts),
      end_year: null,
    };
  }

  function extractYear(texts) {
    for (const t of texts) {
      const match = t.match(/(\d{4})/);
      if (match) return parseInt(match[1]);
    }
    return null;
  }

  // ── Sync ──

  async function syncProfile() {
    const url = normalizeLinkedInUrl(window.location.href);
    if (!url) return;

    updateSideTab('syncing');

    const data = extractProfile();
    if (!data.first_name) {
      updateSideTab('error', { message: '未能提取到姓名，页面可能尚未加载完成' });
      return;
    }

    lastSyncedUrl = url;
    const displayName = `${data.first_name} ${data.last_name}`.trim();

    try {
      const resp = await fetch(`${API_BASE}/api/people/enrich-from-linkedin`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(data),
      });

      if (!resp.ok) {
        updateSideTab('error', { message: `服务端错误 ${resp.status}` });
        return;
      }

      const result = await resp.json();
      updateSideTab(result.action, {
        person_id: result.person_id,
        name: displayName,
        headline: data.headline,
      });
    } catch (e) {
      updateSideTab('offline');
    }
  }

  // ── Utils ──

  function normalizeLinkedInUrl(url) {
    try {
      const u = new URL(url);
      if (!u.hostname.includes('linkedin.com')) return '';
      const path = u.pathname.replace(/\/$/, '');
      if (!path.startsWith('/in/')) return '';
      return `https://www.linkedin.com${path}`;
    } catch {
      return '';
    }
  }

  function esc(str) {
    return String(str || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  // ── Start ──
  if (window.location.pathname.startsWith('/in/')) {
    init();
  }
})();
