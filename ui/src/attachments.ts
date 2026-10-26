import type { Attachment } from './api';
import { tr } from './language';

export type DraftAttachment = Attachment & { size: number };
export const imageTypes = ['image/png', 'image/jpeg', 'image/webp'];

export async function readImages(files: File[], existing: DraftAttachment[]): Promise<DraftAttachment[]> {
  if (files.length + existing.length > 3) throw new Error(tr('Можно прикрепить не более трёх изображений.', 'You can attach at most three images.'));
  if (files.some(file => !imageTypes.includes(file.type))) throw new Error(tr('Выберите изображение PNG, JPEG или WebP.', 'Choose a PNG, JPEG, or WebP image.'));
  if (files.some(file => !file.size || file.size > 2 * 1024 * 1024)) throw new Error(tr('Размер каждого изображения — от 1 байта до 2 МБ.', 'Each image must be from 1 byte to 2 MB.'));
  if ([...files, ...existing].reduce((sum, file) => sum + file.size, 0) > 4 * 1024 * 1024) {
    throw new Error(tr('Общий размер изображений не должен превышать 4 МБ.', 'The total image size must not exceed 4 MB.'));
  }
  const result: DraftAttachment[] = [];
  for (const file of files) {
    const url = await new Promise<string>((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result));
      reader.onerror = () => reject(new Error(tr('Не удалось прочитать изображение. Выберите файл ещё раз.', 'Could not read the image. Choose the file again.')));
      reader.readAsDataURL(file);
    });
    await new Promise<void>((resolve, reject) => {
      const image = new Image();
      image.onload = () => {
        if (image.width > 8192 || image.height > 8192 || image.width * image.height > 16000000) {
          reject(new Error(tr('Изображение слишком большое: до 16 мегапикселей и до 8192 пикселей по стороне.', 'The image is too large: up to 16 megapixels and 8192 pixels per side.')));
        } else resolve();
      };
      image.onerror = () => reject(new Error(tr('Не удалось прочитать изображение. Проверьте файл.', 'Could not read the image. Check the file.')));
      image.src = url;
    });
    result.push({ name: file.name.slice(0, 200), mimeType: file.type, data: url.slice(url.indexOf(',') + 1), size: file.size });
  }
  return result;
}
