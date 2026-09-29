/* File-scoped review queue shared by unidentified files and transfer history. */
(function () {
  'use strict';
  let queue = [], current = null, selection = null, fingerprint = null;
  let generation = 0, request = null, submitting = false, returnFocus = null;
  const modal = '#special-confirmation-modal';
  const status = message => $('#special-status').text(message);
  const link = (url, label) => $('<a>', {href: url, target: '_blank', rel: 'noopener noreferrer'}).text(label);

  function resetChoice() {
    selection = null;
    $('#special-reviewed').prop('checked', false).prop('disabled', true);
    $('#special-submit').prop('disabled', true);
    $('#special-preview').empty();
  }
  function clearDetail() {
    resetChoice();
    $('#special-detail').prop('hidden', true);
    $('#special-episode-list, #special-work-detail').empty();
  }
  // Superseded reads are aborted and guarded; writes are never automatically retried.
  function call(payload, done) {
    const version = ++generation;
    if (request) request.abort();
    status(payload.stage === 'confirm' ? '正在重新识别并整理，请稍候…' : '正在查询 TMDB…');
    request = $.ajax({type: 'POST', url: 'do', dataType: 'json', timeout: payload.stage === 'confirm' ? 0 : 60000,
      data: {cmd: 'special_confirmation', data: JSON.stringify(Object.assign({}, current, payload))}})
      .done(function (result) {
        if (version !== generation) return;
        if (result.retcode !== 0) {
          status(result.retmsg || result.msg || '查询失败，请重试；如登录已过期，请重新登录。');
          return;
        }
        status('');
        done(result);
      }).fail(function (_, reason) {
        if (version === generation && reason !== 'abort') {
          if (payload.stage === 'confirm') resetChoice();
          status(payload.stage === 'confirm' ? '连接中断，整理结果尚未确认。请先刷新记录核实，避免重复操作。' : 'TMDB 加载失败，请点击查询重试。');
        }
      }).always(function () {
        if (version !== generation) return;
        request = null;
        if (submitting) {
          submitting = false;
          $(modal).find('input, select, button').prop('disabled', false);
          $('#special-reviewed').prop('disabled', !selection);
          $('#special-submit').prop('disabled', !selection || !$('#special-reviewed').prop('checked'));
        }
      });
  }
  function search(query) {
    clearDetail();
    fingerprint = null;
    $('#special-work-list').empty();
    call({stage: 'search', query: query}, function (result) {
      fingerprint = result.fingerprint;
      $('#special-query').val(result.query);
      status(result.works.length ? '请选择作品，再核对对应单集。' : '未找到相关作品。请修改名称后重新查询。');
      result.works.forEach(function (work) {
        const row = $('<div>', {class: 'list-group-item'});
        const label = $('<label>', {class: 'form-check'});
        const radio = $('<input>', {type: 'radio', name: 'special-work', class: 'form-check-input'});
        radio.on('change', () => loadDetail(work));
        label.append(radio, $('<span>', {class: 'form-check-label'}).text(`${work.title} · ${work.date || '日期未知'} · ${work.type === 'tv' ? '电视剧' : '电影'} · TMDB ${work.tmdb_id}`));
        row.append(label, $('<p>', {class: 'text-muted text-break mb-1'}).text(work.overview || '暂无简介'), link(work.link, '在 TMDB 查看作品 ↗'));
        $('#special-work-list').append(row);
      });
    });
  }
  function choose(work, season, episode) {
    resetChoice();
    selection = {type: work.type, tmdb_id: work.tmdb_id, season: season, episode: episode ? episode.episode : null};
    $('#special-preview').text(`${current.filename} → ${work.title}${episode ? ` · S${String(season).padStart(2, '0')}E${String(episode.episode).padStart(2, '0')} · ${episode.title}` : ' · 电影'}`);
    $('#special-reviewed').prop('disabled', false);
  }
  function loadDetail(work, season) {
    clearDetail();
    const payload = {stage: 'detail', type: work.type, tmdb_id: work.tmdb_id};
    if (season !== undefined) payload.season = season;
    call(payload, function (result) {
      $('#special-detail').prop('hidden', false);
      $('#special-work-detail').append($('<h6>').text(result.work.title), $('<p>', {class: 'text-break'}).text(result.work.overview || '暂无简介'), link(result.work.link, '在 TMDB 核对作品 ↗'));
      $('#special-season-wrap, #special-episodes').prop('hidden', work.type !== 'tv');
      if (work.type !== 'tv') { choose(result.work, null, null); return; }
      const select = $('#special-season').empty();
      result.seasons.forEach(s => select.append($('<option>', {value: s}).text(s === 0 ? '特别篇（第 0 季）' : `第 ${s} 季`)));
      select.val(result.season).off('change').on('change', function () { loadDetail(work, Number(this.value)); });
      status(result.episodes.length ? '请选择对应单集，可打开 TMDB 详情进行核对。' : '本季暂无单集，请切换其他季或暂不处理。');
      result.episodes.forEach(function (episode) {
        const row = $('<div>', {class: 'list-group-item'});
        const label = $('<label>', {class: 'form-check'});
        const radio = $('<input>', {type: 'radio', name: 'special-episode', class: 'form-check-input'});
        radio.on('change', () => choose(result.work, result.season, episode));
        label.append(radio, $('<span>', {class: 'form-check-label'}).text(`第 ${episode.episode} 集 · ${episode.title} · ${episode.date || '播出日期未知'}`));
        row.append(label, $('<p>', {class: 'text-muted text-break mb-1'}).text(episode.overview || '暂无简介'), link(episode.link, '在 TMDB 查看单集 ↗'));
        $('#special-episode-list').append(row);
      });
    });
  }
  function next() {
    current = queue.shift();
    if (!current) { $(modal).modal('hide'); return; }
    $('#special-source').text(current.filename);
    $('#special-query').val('');
    search('');
  }
  window.SpecialConfirmation = {open: function (items, message) {
    $("#special-batch-result").text(message || "特殊集需要核对 TMDB 对应关系。");
    returnFocus = document.activeElement;
    queue = items.slice();
    $(modal).modal('show');
    next();
  }};
  $(function () {
    $('#special-search').on('click', () => search($('#special-query').val()));
    $('#special-query').on('keydown', function (event) {
      if (event.key === 'Enter' && !event.originalEvent.isComposing) { event.preventDefault(); search(this.value); }
    });
    $('#special-clear').on('click', function () {
      ++generation;
      if (request) request.abort();
      clearDetail();
      $('#special-work-list').empty();
      $('#special-query').val('').trigger('focus');
      status('请输入作品名称后查询。');
    });
    $('#special-reviewed').on('change', function () { $('#special-submit').prop('disabled', !this.checked || !selection); });
    $('#special-skip').on('click', next);
    $('#special-submit').on('click', function () {
      if (submitting || !selection || !$('#special-reviewed').prop('checked')) return;
      submitting = true;
      $(modal).find('input, select, button').prop('disabled', true);
      call(Object.assign({stage: 'confirm', confirmed: true, fingerprint: fingerprint}, selection), function () {
        const page = current.flag;
        // Finish this request before advancing so its cleanup cannot unlock a newer write.
        submitting = false;
        $(modal).find('input, select, button').prop('disabled', false);
        navmenu(page);
        if (queue.length) next();
        else {
          $(modal).one('hidden.bs.modal', () => show_success_modal('已按确认的 TMDB 信息重新识别并整理完成。')).modal('hide');
        }
      });
    });
    $(modal).on('hide.bs.modal', function (event) {
      if (submitting) { event.preventDefault(); return; }
      ++generation;
      if (request) request.abort();
      queue = [];
    }).on('shown.bs.modal', () => $('#special-query').trigger('focus'))
      .on('hidden.bs.modal', function () {
        if (returnFocus && returnFocus.isConnected) returnFocus.focus();
      });
  });
})();
