const paths = {
  cube: <><path d="m12 3 9 5v8l-9 5-9-5V8l9-5Z" /><path d="m3 8 9 5 9-5M12 13v8M7.5 5.5l9 5" /></>,
  plus: <path d="M12 5v14M5 12h14" />,
  settings: <><path d="m9 4 .7-2h4.6l.7 2 2 .9 2.1-.4 2.3 4-1.4 1.6v2.3l1.4 1.6-2.3 4-2.1-.4-2 .9-.7 2H9.7l-.7-2-2-.9-2.1.4-2.3-4L4 12.4v-2.3L2.6 8.5l2.3-4L7 4.9 9 4Z" /><circle cx="12" cy="11.3" r="3" /></>,
  send: <path d="M12 19V5m-6 6 6-6 6 6" />,
  stop: <rect x="6" y="6" width="12" height="12" rx="2" />,
  copy: <><rect x="8" y="8" width="12" height="12" rx="3" /><path d="M15 8V5a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h3" /></>,
  check: <path d="m5 12 4 4L19 6" />,
  code: <path d="m8 7-5 5 5 5m8-10 5 5-5 5m-3-13-2 16" />,
  tree: <><rect x="9" y="3" width="6" height="5" rx="1" /><path d="M12 8v5M5 16v-3h14v3" /><rect x="2" y="16" width="6" height="5" rx="1" /><rect x="16" y="16" width="6" height="5" rx="1" /></>,
  image: <><rect x="3" y="3" width="18" height="18" rx="4" /><circle cx="8" cy="8" r="1.5" /><path d="m3 17 5-5 4 4 4-6 5 7" /></>,
  ruler: <><path d="m4 14 10-10 6 6-10 10-6-6Z" /><path d="m10 8 2 2m2-6 2 2m-10 6 2 2" /></>,
};

export default function Icon({ name, size = 20 }: { name: keyof typeof paths; size?: number }) {
  return <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor"
    strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{paths[name]}</svg>;
}
