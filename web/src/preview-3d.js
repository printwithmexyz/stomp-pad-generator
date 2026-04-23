// three.js STL viewer. Lazy-imported so users who don't render STL never
// pay the ~600 KB three.js cost.

export async function mount3dPreview(container, stlBytes) {
  const [THREE, { STLLoader }, { OrbitControls }] = await Promise.all([
    import('three'),
    import('three/examples/jsm/loaders/STLLoader.js'),
    import('three/examples/jsm/controls/OrbitControls.js'),
  ]);

  const w = container.clientWidth || 320;
  const h = container.clientHeight || 240;

  const scene = new THREE.Scene();
  scene.background = new THREE.Color(0xf5f5f7);

  const camera = new THREE.PerspectiveCamera(45, w / h, 0.1, 5000);

  const renderer = new THREE.WebGLRenderer({ antialias: true });
  renderer.setSize(w, h);
  renderer.setPixelRatio(window.devicePixelRatio);
  container.appendChild(renderer.domElement);

  scene.add(new THREE.AmbientLight(0xffffff, 0.55));
  const key = new THREE.DirectionalLight(0xffffff, 0.85);
  key.position.set(1, 1.5, 1);
  scene.add(key);
  const fill = new THREE.DirectionalLight(0xffffff, 0.35);
  fill.position.set(-1, -0.5, -1);
  scene.add(fill);

  const loader = new STLLoader();
  const geometry = loader.parse(stlBytes.buffer);
  geometry.computeVertexNormals();
  geometry.center();
  const material = new THREE.MeshPhongMaterial({
    color: 0x007aff,
    specular: 0x111111,
    shininess: 80,
    flatShading: false,
  });
  const mesh = new THREE.Mesh(geometry, material);
  scene.add(mesh);

  const box = new THREE.Box3().setFromObject(mesh);
  const size = box.getSize(new THREE.Vector3());
  const maxDim = Math.max(size.x, size.y, size.z) || 1;
  camera.position.set(maxDim * 1.2, maxDim * 1.2, maxDim * 1.6);
  camera.lookAt(0, 0, 0);

  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.dampingFactor = 0.1;

  let running = true;
  function tick() {
    if (!running) return;
    controls.update();
    renderer.render(scene, camera);
    requestAnimationFrame(tick);
  }
  tick();

  return {
    dispose() {
      running = false;
      controls.dispose();
      geometry.dispose();
      material.dispose();
      renderer.dispose();
      if (renderer.domElement.parentNode === container) {
        container.removeChild(renderer.domElement);
      }
    },
  };
}
