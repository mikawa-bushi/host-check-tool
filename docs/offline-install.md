# 閉域網 (オフライン) の PC への導入

工場のエッジ PC やライン制御 PC はインターネットに接続できないことが多いため、
ネット接続できる PC で「wheelhouse」(procdiag と依存パッケージの一式) を作り、
USB メモリ等で持ち込んでインストールします。

## 1. 導入先 PC の情報を確認する

導入先 PC で次を確認します (Python 3.10 以上が必要です)。

```bash
python3 --version   # 例: Python 3.10.12 → 版数は 3.10
uname -m            # 例: x86_64 / aarch64
```

## 2. ネット接続できる PC で wheelhouse を作る

このリポジトリを取得した Linux / macOS / WSL の PC で、導入先の版数と CPU を指定して実行します。

```bash
bin/build_wheelhouse.sh 3.10 x86_64
# → procdiag-wheelhouse-py3.10-x86_64.tar.gz
```

psutil・pydantic などの C 拡張は導入先向けのバイナリ (manylinux2014 = glibc 2.17 以上。
RHEL/CentOS 7 以降、Ubuntu 18.04 以降、Debian 9 以降) を取得するため、作成する PC の
OS や CPU は導入先と違っていても構いません。

## 3. 導入先 PC でインストールする

```bash
tar xzf procdiag-wheelhouse-py3.10-x86_64.tar.gz
./procdiag-wheelhouse-py3.10-x86_64/install.sh
# → ~/procdiag-venv/bin/procdiag
```

`install.sh` は既定で `~/procdiag-venv` に仮想環境を作って導入します
(システムの Python を汚しません)。システムの環境に入れる場合は `install.sh --system`。

導入後は README の Quick Start と同じ手順で使えます。まずは記録なしで

```bash
~/procdiag-venv/bin/procdiag snapshot
```

を実行すると、その場でホストの状態を確認できます。

## 4. 解析結果を持ち帰る

```bash
~/procdiag-venv/bin/procdiag bundle
```

で記録 DB・レポート・現在の状態を 1 つの tar.gz にまとめられます
(秘密情報を含み得るプロセスのコマンドラインは既定で削除されます)。
