const EMPTY = Object.freeze([]);
const MIB = 1024 * 1024;

export function videoFigure({name, mime, url}) {
  const figure = document.createElement('figure');
  figure.style.margin = '0.6rem 0';
  figure.style.maxWidth = '100%';
  const video = document.createElement('video');
  video.controls = true;
  video.playsInline = true;
  video.preload = 'none';
  video.style.maxWidth = '100%';
  video.style.maxHeight = '55vh';
  video.setAttribute('aria-label', name);
  const source = document.createElement('source');
  source.type = mime;
  source.src = url;
  const caption = document.createElement('figcaption');
  caption.textContent = name;
  const error = document.createElement('p');
  error.hidden = true;
  error.setAttribute('role', 'status');
  error.textContent = '当前浏览器无法播放此视频。';
  const download = document.createElement('a');
  download.textContent = '下载视频';
  download.download = name;
  download.href = url;
  error.append(' ', download);
  const failed = () => { error.hidden = false; };
  source.addEventListener('error', failed);
  video.addEventListener('error', failed);
  video.append(source);
  figure.append(video, caption, error);
  return figure;
}

export async function encodeVideos(files) {
  const items = [];
  for (const file of files) {
    const data = await new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(reader.result.slice(reader.result.indexOf(',') + 1));
      reader.onerror = () => reject(reader.error);
      reader.onabort = () => reject(new Error('视频读取已中止'));
      reader.readAsDataURL(file);
    });
    items.push({name: file.name, data});
  }
  return items;
}

export class VideoDrafts {
  constructor(form, session, changed, preview, showError) {
    this.session = session;
    this.changed = changed;
    this.preview = preview;
    this.bySession = new Map();
    this.list = document.createElement('div');
    this.list.setAttribute('aria-label', '待发送的视频');
    this.list.hidden = true;
    this.input = document.createElement('input');
    this.input.type = 'file';
    this.input.id = 'video-files';
    this.input.hidden = true;
    this.input.multiple = true;
    this.input.setAttribute('aria-label', '选择视频文件');
    this.add = document.createElement('button');
    this.add.type = 'button';
    this.add.className = 'quiet';
    this.add.textContent = '添加视频';
    this.add.hidden = true;
    this.add.disabled = true;
    const controls = form.querySelector('.compose-tools');
    controls.prepend(this.add);
    controls.before(this.input, this.list);
    this.add.onclick = () => {
      this.owner = session();
      this.input.click();
    };
    this.input.onchange = () => {
      const sid = this.owner || session();
      this.owner = null;
      try {
        if (sid && this.input.files.length) this.stage(sid, Array.from(this.input.files));
      } catch (error) {
        showError(error);
      } finally {
        this.input.value = '';
      }
    };
  }

  configure(limits) {
    this.limits = limits;
    this.add.hidden = !limits;
    if (limits) this.input.accept = Object.keys(limits.formats).join(',');
  }

  files(sid) {
    return this.bySession.get(sid) || EMPTY;
  }

  mime(file) {
    const suffix = file.name.slice(file.name.lastIndexOf('.')).toLowerCase();
    return this.limits.formats[suffix];
  }

  stage(sid, added) {
    const limits = this.limits;
    const files = [...this.files(sid), ...added];
    if (files.length > limits.max_items) throw new Error(`每条消息最多可附加 ${limits.max_items} 项`);
    for (const file of added) {
      if (!this.mime(file)) throw new Error('请选择 MP4、MOV 或 WebM 视频');
      if (!file.size || file.size > limits.max_bytes) {
        throw new Error(`每个视频需大于零且小于或等于 ${limits.max_bytes / MIB} MiB`);
      }
    }
    if (files.reduce((bytes, file) => bytes + file.size, 0) > limits.max_total) {
      throw new Error(`一条消息的视频总大小最多为 ${limits.max_total / MIB} MiB`);
    }
    this.set(sid, files);
  }

  set(sid, files) {
    if (files.length) this.bySession.set(sid, files);
    else this.bySession.delete(sid);
    if (sid === this.session()) {
      this.render();
      this.changed();
    }
  }

  remove(sid, files) {
    if (files.length) this.set(sid, this.files(sid).filter(file => !files.includes(file)));
  }

  render() {
    const sid = this.session();
    const files = this.files(sid);
    this.list.replaceChildren();
    this.list.hidden = !files.length;
    for (const file of files) {
      const row = document.createElement('div');
      const open = document.createElement('button');
      open.type = 'button';
      open.className = 'quiet';
      open.textContent = file.name;
      open.setAttribute('aria-label', '预览 ' + file.name);
      open.onclick = () => this.preview(file, this.mime(file));
      const remove = document.createElement('button');
      remove.type = 'button';
      remove.className = 'quiet';
      remove.textContent = '移除';
      remove.setAttribute('aria-label', '移除 ' + file.name);
      remove.onclick = () => this.remove(sid, [file]);
      row.append(open, remove);
      this.list.append(row);
    }
  }
}
