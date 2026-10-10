export const SUDOKU_PAGE = `<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>数独</title>
<style>
  body { margin: 0; min-height: 100vh; display: flex; flex-direction: column; align-items: center; justify-content: center; background: #15171c; color: #e8e8e8; font-family: "Microsoft YaHei", system-ui, sans-serif; }
  #status { margin-bottom: 16px; color: #9ab; font-size: 18px; letter-spacing: 1px; }
  table { border-collapse: collapse; }
  td { width: 52px; height: 52px; text-align: center; font-size: 30px; border: 1px solid #3a3f4a; }
  td.br { border-right: 3px solid #7a8190; }
  tr.bb td { border-bottom: 3px solid #7a8190; }
  td.given { color: #ffffff; font-weight: bold; }
  td.player { color: #7fd1ff; }
  td.conflict { background: #5a1f1f; color: #ff9a9a; }
</style>
</head>
<body>
<div id="status">连接中…</div>
<table id="board"></table>
<script>
  var statusEl = document.getElementById('status');
  var boardEl = document.getElementById('board');
  var LABEL = { none: '还没有开局', playing: '进行中', solved: '已完成' };

  function render(snap) {
    var conflicts = {};
    (snap.conflicts || []).forEach(function (c) { conflicts[c.row + ',' + c.col] = true; });
    statusEl.textContent = LABEL[snap.status] + (snap.status === 'none' ? '' : '  ·  已填 ' + snap.filled + '/81');
    boardEl.innerHTML = '';
    for (var r = 0; r < 9; r++) {
      var tr = document.createElement('tr');
      if (r % 3 === 2 && r < 8) tr.className = 'bb';
      for (var c = 0; c < 9; c++) {
        var td = document.createElement('td');
        var value = snap.board.length ? snap.board[r][c] : 0;
        var classes = [];
        td.textContent = value ? String(value) : '';
        if (snap.givens.length && snap.givens[r][c]) classes.push('given');
        else if (value) classes.push('player');
        if (conflicts[(r + 1) + ',' + (c + 1)]) classes.push('conflict');
        if (c % 3 === 2 && c < 8) classes.push('br');
        td.className = classes.join(' ');
        tr.appendChild(td);
      }
      boardEl.appendChild(tr);
    }
  }

  var source = new EventSource('/events');
  source.onerror = function () { statusEl.textContent = '连接断开，正在重连…'; };
  source.addEventListener('board', function (e) { render(JSON.parse(e.data)); });
</script>
</body>
</html>
`;
