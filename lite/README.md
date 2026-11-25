# hipocampo lite (macOS)

Esta pasta traz peças do hipocampo adaptadas ao macOS e a quem usa a memória nativa do
Claude Code com um índice por projeto. Todo o código está em `memoria.py`, que usa apenas a
biblioteca padrão do Python (3.9 ou mais recente) e não depende de ferramentas GNU.

## Comandos

- `memoria.py hook` roda no `SessionStart`. Descobre qual `MEMORY.md` a sessão carregou e
  avisa o modelo quando o índice será cortado (200 linhas ou 25.000 unidades UTF-16), quando
  restam menos de 15 linhas até o corte, quando uma linha aponta para um arquivo inexistente
  ou quando alguma memória não é citada por nenhuma linha. Se nada disso acontece, não
  imprime nada; um erro interno também termina em silêncio e nunca bloqueia a sessão.
- `memoria.py check [PROJECTS]` faz a mesma auditoria em todos os projetos e sai com código 1
  se algum índice tiver problema.
- `memoria.py backup [PROJECTS] --dest DIR [--exclude TEXTO ...]` copia os `.md` de
  `PROJECTS/*/memory/` para `DIR/<projeto>/`, faz commit quando algo mudou e, se o repositório
  de `DIR` tiver um remote, faz push a cada execução, o que reenvia um push que falhou antes.
  Projetos cujo nome contém `TEXTO` ficam de fora, mas o que já foi enviado continua no
  histórico. `DIR` precisa estar vazio na primeira vez; o arquivo `.memoria-backup` registra
  qual `PROJECTS` ele espelha, e qualquer outra pasta é recusada antes de algo ser apagado.
  Symlinks dentro da memória não são copiados.

`PROJECTS` é, por padrão, `$CLAUDE_CONFIG_DIR/projects` ou `~/.claude/projects`.

## Como o hook encontra a memória

Uma sessão aberta dentro de um repositório git, inclusive num worktree, carrega a memória da
raiz do repositório principal; fora de um repositório, carrega a da pasta em que o Claude foi
aberto. Na pasta `~/.claude/projects`, o nome de cada projeto troca por `-` todo caractere que
não seja letra ou número: `Ju&Mi` vira `Ju-Mi`, e um espaço também vira `-`.

## Instalação

Acrescente as duas entradas abaixo à lista de `SessionStart` de `~/.claude/settings.json`,
sem apagar as que já existem, trocando `<PASTA>` pelo caminho deste repositório e `<BACKUP>`
pela pasta do backup:

```json
{ "matcher": "startup", "hooks": [
  { "type": "command", "command": "python3 <PASTA>/lite/memoria.py hook 2>/dev/null || true", "timeout": 10 },
  { "type": "command", "command": "python3 <PASTA>/lite/memoria.py backup --dest <BACKUP> >/dev/null 2>&1 || true", "timeout": 60 }
]}
```

As memórias guardam dados pessoais. Se o backup for para um servidor remoto, use um
repositório privado (`git -C <BACKUP> remote add origin <URL>`) e deixe os projetos de
trabalho de fora com `--exclude`.

## Suíte original no macOS

Os scripts do upstream foram escritos para bash 4 ou mais recente e para as ferramentas GNU.
Depois de `brew install bash coreutils gawk flock`, o `.githooks/pre-commit` põe essas
ferramentas no `PATH` por conta própria. Para rodar o smoke test à mão:

```bash
PATH="$(brew --prefix)/opt/gawk/libexec/gnubin:$(brew --prefix)/opt/coreutils/libexec/gnubin:$PATH" bash tests/smoke.sh
```

O `scripts/tarefa-vigia.sh` continua só para Linux, porque mede a CPU dos processos lendo
`/proc`, e por isso não foi portado.

## Testes

```bash
python3 -m unittest discover -s lite
```
