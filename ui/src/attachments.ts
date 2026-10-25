import type { Attachment } from './api';

export type DraftAttachment = Attachment & { size: number };
export const imageTypes = ['image/png', 'image/jpeg', 'image/webp'];

export async function readImages(files: File[], existing: DraftAttachment[]): Promise<DraftAttachment[]> {
  if (files.length + existing.length > 3) throw new Error('Можно прикрепить не более трёх изображений.');
  if (files.some(file => !imageTypes.includes(file.type))) throw new Error('Выберите изображение PNG, JPEG или WebP.');
  if (files.some(file => !file.size || file.size > 2 * 1024 * 1024)) throw new Error('Размер каждого изображения — от 1 байта до 2 МБ.');
  if ([...files, ...existing].reduce((sum, file) => sum + file.size, 0) > 4 * 1024 * 1024) {
    throw new Error('Общий размер изображений не должен превышать 4 МБ.');
  }
  const result: DraftAttachment[] = [];
  for (const file of files) {
    const url = await new Promise<string>((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result));
      reader.onerror = () => reject(new Error('Не удалось прочитать изображение. Выберите файл ещё раз.'));
      reader.readAsDataURL(file);
    });
    await new Promise<void>((resolve, reject) => {
      const image = new Image();
      image.onload = () => {
        if (image.width > 8192 || image.height > 8192 || image.width * image.height > 16000000) {
          reject(new Error('Изображение слишком большое: до 16 мегапикселей и до 8192 пикселей по стороне.'));
        } else resolve();
      };
      image.onerror = () => reject(new Error('Не удалось прочитать изображение. Проверьте файл.'));
      image.src = url;
    });
    result.push({ name: file.name.slice(0, 200), mimeType: file.type, data: url.slice(url.indexOf(',') + 1), size: file.size });
  }
  return result;
}
