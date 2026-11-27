# HeadTrack: controle pelo celular, áudio binaural no PC

A página pública [HeadTrack](https://brianlahr.netlify.app/headtrack/) capta **orientação**, não áudio. Um programa Python local executa o checkpoint `spatial_tcn_v1` a 44,1 kHz, renderiza as duas fontes com o mesmo SOFA KEMAR e reproduz o som nos fones do PC. O checkpoint e o SOFA permanecem locais. A página funciona sozinha para inspecionar o sensor e testar o yaw manual; para ouvir a separação, é preciso ligar o programa local e conectar a página.

## Preparação na máquina pessoal (PowerShell)

Escolha a pasta `Mestrado` que você já utiliza. O motor Python agora está no próprio clone do portfólio em `my-portfolio\headtrack\engine`; versões novas chegam com `git pull`. Abra PowerShell e ajuste a primeira linha ao caminho real:

```powershell
$base = "C:\CAMINHO\PARA\Mestrado"
cd $base
git clone https://github.com/joaocolombari/yaw-walk-with-me.git
git clone https://github.com/BrianLahr/my-portfolio.git
git clone --branch research/cec2-pilot-audit-2026-11-24 https://github.com/BrianLahr/mestrado-eesc.git mestrado-eesc-imu
cd .\mestrado-eesc-imu
git lfs install
git lfs pull
cd ..
```

Se já existir algum clone, use `git -C "$base\my-portfolio" pull --ff-only origin main` em vez de clonar novamente. Confira que o arquivo baixado é realmente um checkpoint, e não um ponteiro de texto LFS: `(Get-Item "$base\mestrado-eesc-imu\notebooks\ears_kemar_tcn_causal_best.pt").Length` deve ser maior que 132 bytes.

Use o `KEMAR_s.sofa` já disponível em `F:\Brian\Datasets\MMHR-HRIR\KEMAR_s.sofa`. A aplicação verifica seu MD5 antes de reproduzir.

Na máquina pessoal do Brian, use o `.venv` com Python 3.10.11 já existente; o código atualizado vem do clone:

```powershell
cd "$base\imu_binaural_demo"
.\.venv\Scripts\Activate.ps1
python --version
python -m pip install -r "$base\my-portfolio\headtrack\engine\requirements.txt"
python -m pip install -r "$base\my-portfolio\headtrack\requirements-bridge.txt"
```

Se PowerShell bloquear a ativação, execute `Set-ExecutionPolicy -Scope Process Bypass` na sessão atual. Confira os cinco caminhos antes de executar:

```powershell
Test-Path "$base\my-portfolio\headtrack\engine\run.py"
Test-Path "$base\yaw-walk-with-me\aar_core.py"
Test-Path "$base\mestrado-eesc-imu\notebooks\ears_kemar_tcn_causal_best.pt"
Test-Path "F:\Brian\Datasets\MMHR-HRIR\KEMAR_s.sofa"
Test-Path "$base\my-portfolio\headtrack\bridge.py"
```

Os cinco resultados precisam ser `True`. Inicie o áudio local **nesta janela**:

```powershell
python "$base\my-portfolio\headtrack\bridge.py" --joao-repo "$base\yaw-walk-with-me" --checkpoint "$base\mestrado-eesc-imu\notebooks\ears_kemar_tcn_causal_best.pt" --sofa "F:\Brian\Datasets\MMHR-HRIR\KEMAR_s.sofa"
```

Se `laura.mp3` ou `cooper.mp3` falharem ao abrir, converta-os para WAV PCM 44,1 kHz mono e passe `--source-a` e `--source-b`. Conecte fones estéreo ao PC; se o dispositivo padrão estiver errado, use `python -m sounddevice` e depois `--output-device "NOME"`. Comece com CPU; mudar para `--device cuda:0` só faz sentido após medir os prazos nesse PC. Ao iniciar, o programa seleciona um trecho sustentado de fala nos primeiros 30 s de cada arquivo, mede p95 do DSP com 1/2/4 threads e escolhe a melhor configuração medida. Depois informa o instante inicial, RMS seco, RMS após KEMAR, ΔSI-SDR por ouvido e consistência da mistura. Se o alvo continuar abaixo de −60 dBFS, a separação medida pode ser inaudível. Para testar a TCN com duas janelas por chamada, passe `--block-hops 2`: o prazo por lote passa de 5,80 a 11,61 ms, e o atraso relativo programado entre mistura e realce de 11,61 a 17,41 ms. O teste de equivalência do streaming roda ao iniciar; compare p95 e novos esvaziamentos da fila durante vários minutos antes de concluir se ajudou. `--buffer-blocks 16` (padrão, até ~93 ms na fila) ou 32 amortecem interrupções curtas. `--audio-latency high` (padrão) solicita um buffer estável ao driver. A fila e a placa aumentam a latência absoluta; não tornam uma carga média acima do prazo viável em tempo real. Não passe `--demo-dir` apontando para o ZIP antigo, pois isso carregaria o código desatualizado.

## Conexão segura entre celular e PC

A página é HTTPS. Para conectar o WebSocket a um serviço no PC sem mudar a configuração da rede, instale `cloudflared` pelo [instalador Windows da Cloudflare](https://developers.cloudflare.com/tunnel/downloads/) e abra **um segundo PowerShell**:

```powershell
cloudflared tunnel --url http://localhost:8765
```

O comando mostra uma URL temporária `https://…trycloudflare.com`. No celular, abra `https://brianlahr.netlify.app/headtrack/` em Safari/Chrome, toque em **Ativar sensores** e conceda permissão. Cole na página somente a URL do túnel e o **código de sessão** exibido pelo `bridge.py` na primeira janela. Toque em **Conectar**.

A URL do túnel muda a cada reinício. O serviço local escuta somente `127.0.0.1`, valida a origem exata da página, exige um segredo aleatório criado a cada execução e permite um único controlador. Não cole o código de sessão no GitHub, em capturas de tela ou na URL. Quando a conexão termina, o programa silencia a saída até outra conexão autenticada; Ctrl+C encerra o processamento. O túnel transporta **apenas comandos** (yaw, ganhos e seleção), sem áudio ou checkpoint. Se publicar o site em outro domínio, inicie o programa com `--origin "https://SEU-DOMINIO"`.

## Geometria e limites da demonstração

Mantenha o celular em posição **vertical**, tela voltada para você, com a câmera traseira apontando para a frente. O eixo de escuta virtual é `-z` no referencial do dispositivo. Calibre olhando para o meio entre as fontes. Faça uma rotação à esquerda e confira o sinal de yaw; existe um botão de inversão. Um celular segurado na mão mede a **orientação do celular**; só representa a cabeça se girar rigidamente com ela. Use o modo manual para inspecionar o sistema sem montá-lo.

O cálculo de orientação projeta o vetor frontal do aparelho no plano horizontal depois da composição `R_z(alpha)R_x(beta)R_y(gamma)` definida pelo padrão W3C. Quando o eixo frontal aponta quase na vertical, o yaw não é definido e a página pede para erguer o telefone. Um navegador pode fornecer referência relativa com deriva; recalibre com frequência. Permissão de sensores exige HTTPS e interação do usuário. Notificações de yaw passam por internet e túnel: a idade da última mensagem aparece no site, **não** é uma medição de latência ponta a ponta.

As fontes são as vozes gravadas do João. O som misturado e o processado são reproduzidos pelo mesmo PC; a diferença relativa de **11,61 ms programada** é a simulação da escuta acústica direta versus a via processada. O atraso real da placa e da rede não é fixo. A rede treinada para A em +30° e B em −30° pode perder identidade sob giros; a página sinaliza essa condição, sem extrapolar desempenho do treino. Essa demonstração não usa microfones reais nem executa a TCN no celular.

Os controles da mistura e da fonte separada são em dB; a amplitude é multiplicada por `10^(dB/20)`. O checkbox começa desligado e a fonte A é selecionada, permitindo conferir A/B isoladas primeiro. O foco gaussiano segue o `aar_core.compute_attention` do João: `boost_db = 10 exp(-θ²/(2·20²))`, θ em graus, aplicado somente à imagem separada escolhida. A escolha automática pega a fonte mais próxima do eixo frontal; no empate a 0° escolhe A. João aplica o ganho às vozes originais antes da mistura; aqui a TCN recebe a mistura sem ganho diferencial para preservar a condição de treino. O fator de distância do João vale 1 nesta cena sintética a 1 m.

Na telemetria, “lotes atrasados” são chamadas DSP acima do prazo de 5,80 ms (uma janela) ou 11,61 ms (duas janelas); “fila vazia” conta callbacks sem áudio, enquanto “avisos do driver” conta flags do PortAudio separadamente. A versão anterior somava estes eventos no mesmo contador e podia contá-los duas vezes. O p95 considera até 512 lotes recentes. dBFS de mistura/separado/saída e contagem de blocos limitados ajudam a distinguir pouco sinal de clipping. “Sensor parado” corresponde à idade da última amostra efetiva de orientação; o pulso de controle não disfarça a interrupção do sensor. Com fila vazia recorrente, a escuta não é contínua e não serve para avaliar qualidade nem latência.
