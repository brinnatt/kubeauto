## Y1、简介

WordPress 是一个免费的、开源的内容管理系统(CMS)，建立在 MySQL 数据库和 PHP 语言能力的基础上。由于其可扩展的插件架构和模板系统，它的大部分管理可以通过 web 界面完成。

这就是为什么在创建不同类型的网站时，从博客到产品页面到电子商务网站，WordPress 是一个受欢迎的选择。

搭建 WordPress 通常包括安装 LAMP(Linux, Apache, MySQL, PHP) 或者 LNMP(Linux, Nginx, MySQL, PHP) 等相关技术栈，需要一定的基础能力。

当然，我们下面使用 Docker 和 Docker Compose 来实现 WordPress 的搭建很方便，至于 Docker 是什么，这是一个当今流行且很大的话题。需要花一些时间学习，这里直接上手使用。

接下来的教程中，我们将会安装 MySQL、Nginx、WordPress 以及 Certbot 4 个容器。Certbot 负责通过 [ACME](https://datatracker.ietf.org/doc/html/rfc8555) 协议向 [Let's Encrypt](https://letsencrypt.org/zh-cn/) 申请并续期 TLS 证书；Nginx 加载证书后对外提供 HTTPS，浏览器即可通过 `https://` 安全访问 WordPress。

证书由 Let's Encrypt（ISRG 运营的非营利性 CA）签发。按 [官方 FAQ](https://letsencrypt.org/zh-cn/docs/faq/)，每张证书有效期为 **90 天**（约 3 个月），不会自动延期。到期前需运行 `certbot renew` 续期，因此文末会配置 cron 定时任务。

## Y2、前提条件

为了实现上面我们所说的目的，需要有下面这些前提条件：

+ 操作系统 Ubuntu 20.04 或者 CentOS 8.4，要求有 root 权限和防火墙服务(iptables.service)。
+ 安装 Docker 引擎，参见[官方手册](https://docs.docker.com/engine/install/centos/)。
+ 安装 Docker Compose，安装 Compose 插件或者独立的二进制包，参见[官方手册](https://docs.docker.com/compose/install/linux/)。
+ 注册一个域名，本文通篇会使用该域名。比如本人的域名是 brinnatt.com。
+ 给注册好的域名 brinnatt.com 添加如下两条 A 记录。
  + 使用 brinnatt.com 添加一条 A 记录指向我们的服务器公网地址。
  + 使用 www.brinnatt.com 添加一条 A 记录指向我们的服务器公网地址。

一切准备就绪后，就可以开始我们的部署行动。

## Y3、定义 Web 服务器配置

在开始运行任何容器之前，首先要定义好 Nginx 配置。配置需同时满足两件事：一是把 PHP 请求转发给 WordPress（php-fpm）；二是在 80 端口对外提供 ACME **HTTP-01** 挑战路径 `/.well-known/acme-challenge/`。

理解 HTTP-01 的方向很重要：**Let's Encrypt 的验证服务器会主动访问你的站点**（公网 HTTP GET），而不是「把请求发给 Certbot」。Certbot 的 **webroot 验证插件**（`--webroot`，仅负责**获取**证书，属于 Authenticator，不修改 Nginx）把临时挑战文件写入 Web 根目录；Nginx 负责把这些文件提供给验证服务器。验证通过后，证书写入共享卷 `/etc/letsencrypt`，续期时重复同一流程。

首先为 WordPress 创建一个专门的项目根目录，比如：

```bash
$ mkdir wordpress
```

然后，进入 wordpress 项目根目录：

```bash
$ cd wordpress
```

接着，为 nginx 配置文件创建一个目录：

```bash
$ mkdir nginx-conf
```

最后，编辑配置文件，在这个文件里添加 server 代码块，配置好 server_name 指令、文档根目录、以及其它代码块，用以处理 Certbot 客户端的证书请求，PHP 动态网页和静态资源请求等。

```bash
$ vim nginx-conf/nginx.conf
server {
        listen 80;
        listen [::]:80;

        server_name your_domain www.your_domain;

        index index.php index.html index.htm;

        root /var/www/html;

        location ~ /.well-known/acme-challenge {
                allow all;
                root /var/www/html;
        }

        location / {
                try_files $uri $uri/ /index.php$is_args$args;
        }

        location ~ \.php$ {
                try_files $uri =404;
                fastcgi_split_path_info ^(.+\.php)(/.+)$;
                fastcgi_pass wordpress:9000;
                fastcgi_index index.php;
                include fastcgi_params;
                fastcgi_param SCRIPT_FILENAME $document_root$fastcgi_script_name;
                fastcgi_param PATH_INFO $fastcgi_path_info;
        }

        location ~ /\.ht {
                deny all;
        }
        
        location = /favicon.ico { 
                log_not_found off; access_log off; 
        }
        location = /robots.txt { 
                log_not_found off; access_log off; allow all; 
        }
        location ~* \.(css|gif|ico|jpeg|jpg|js|png)$ {
                expires max;
                log_not_found off;
        }
}
```

注意，要把 `your_domain` 替换成我们自己的域名。该 server 代码块的核心指令如下：

+ `listen`：在 80 端口监听 HTTP。[HTTP-01 挑战](https://datatracker.ietf.org/doc/html/rfc8555#section-8.3)要求 Let's Encrypt 能通过 HTTP（默认端口 80）访问 `http://<域名>/.well-known/acme-challenge/<token>`。此阶段尚未监听 443；证书签发并挂载到卷后，再增加 HTTPS `server` 块，并在 `listen` 上添加 `ssl` 参数（参见 [nginx HTTPS 配置](http://nginx.org/en/docs/http/configuring_https_servers.html)）。

+ `server_name`：声明本 `server` 块处理的虚拟主机名；请求中的 `Host` 头与之匹配时由该块处理。可写多个名字，第一个通常视为主名。请将 `your_domain` 替换为自己的域名。

+ `index`：当请求 URI 映射到目录时，按顺序尝试的默认文件名（参见 [index 指令](http://nginx.org/en/docs/http/ngx_http_index_module.html#index)）。

+ `root`：请求 URI 的文件系统根目录。`/var/www/html` 与 WordPress 官方镜像的 Web 根及 Compose 中 `wordpress` 命名卷挂载点一致，Nginx 与 WordPress 容器通过同一卷共享站点文件。

+ `location ~ /.well-known/acme-challenge`：处理 ACME 挑战路径。`~` 表示正则匹配 location（参见 [location](http://nginx.org/en/docs/http/ngx_http_core_module.html#location)）。Certbot webroot 插件在 `{webroot}/.well-known/acme-challenge/` 写入临时文件；Let's Encrypt 验证服务器从公网发起 HTTP GET 读取内容，以证明你对域名有控制权（域名 A 记录需已解析到本机——验证机制是 HTTP，不是 DNS-01）。正则中未转义的 `.` 可匹配任意单字符；更严谨可用 `^~ /.well-known/acme-challenge/` 前缀匹配，本配置在生产中已验证可用。

+ `location /`：前缀匹配，处理其余路径。`try_files` 按顺序探测文件是否存在，均失败则**内部重定向**到最后一项（参见 [try_files](http://nginx.org/en/docs/http/ngx_http_core_module.html#try_files)）：
  + `$uri`：按 URI 查找文件，如 `/about.html` → `/var/www/html/about.html`
  + `$uri/`：当作目录查找，结合 `index` 尝试默认页，如 `/blog/` → `/var/www/html/blog/index.php`
  + `/index.php$is_args$args`：内部重定向到 `index.php`，并保留原查询串
    + `$is_args`：原 URL 带 `?` 时输出 `?`，否则为空
    + `$args`：原查询参数（不含 `?`）
    + 例：`/search?q=nginx` → 内部重定向为 `/index.php?q=nginx`；`/foo`（无查询串）→ `/index.php`
    + WordPress 通过 FastCGI 环境变量（如 `REQUEST_URI`）识别用户原始路径，permalink 依赖此机制

+ `location ~ \.php$`：正则匹配以 `.php` 结尾的 URI，通过 **FastCGI** 转发给 PHP-FPM（非 HTTP 反向代理）。WordPress 镜像为 `php-fpm` 变体，监听 9000 端口；Nginx 作为 FastCGI 客户端，将 PHP 脚本路径等参数传给 FPM 进程执行（参见 [ngx_http_fastcgi_module](http://nginx.org/en/docs/http/ngx_http_fastcgi_module.html)）。
  + `try_files $uri =404`：仅当对应 `.php` 文件真实存在时才交给 FPM，避免将任意 URI 当作脚本执行（常见安全加固，参见 nginx/PHP 部署实践）。
  + `fastcgi_split_path_info ^(.+\.php)(/.+)$`：将 URI 拆成脚本部分与 PATH_INFO 部分，结果写入 `$fastcgi_script_name`、`$fastcgi_path_info`（参见 [fastcgi_split_path_info](http://nginx.org/en/docs/http/ngx_http_fastcgi_module.html#fastcgi_split_path_info)）。
  + `fastcgi_pass wordpress:9000`：FastCGI 后端地址；`wordpress` 为 Compose 服务名，在 `app-network` 内通过内置 DNS 解析为容器 IP。
  + `fastcgi_index index.php`：URI 以 `/` 结尾时补全的默认脚本名。
  + `include fastcgi_params`：引入标准 FastCGI 参数集（含 `REQUEST_URI`、`QUERY_STRING` 等）。
  + `fastcgi_param SCRIPT_FILENAME`：PHP-FPM 实际执行的文件绝对路径，通常为 `$document_root$fastcgi_script_name`。
  + `fastcgi_param PATH_INFO`：传递给 PHP 的路径后缀信息（PATH_INFO 模式）。

+ `location ~ /\.ht`：拒绝访问 `.htaccess` 等点开头文件。Nginx 不解析 Apache 的 `.htaccess`；`deny all` 拒绝所有客户端（参见 [deny](http://nginx.org/en/docs/http/ngx_http_access_module.html#deny)）。

+ `location = /favicon.ico`，`location = /robots.txt`：`=` 为精确匹配。`log_not_found off` 与 `access_log off` 减少无意义日志。

+ `location ~* \.(css|gif|ico|jpeg|jpg|js|png)$`：`~*` 为不区分大小写的正则匹配。`expires max` 将 `Expires` 设为 2037-12-31（nginx 对 `max` 的约定值），配合关闭访问日志，减轻静态资源开销。

Nginx 配置就绪后，就可以继续创建环境变量，以便在运行时传递给应用程序和数据库。

## Y4、定义环境变量

您的数据库和 WordPress 容器应用程序需要在运行时访问某些环境变量，以便让应用程序的数据可以持久化，并随时可以被应用程序访问到。

要设置的这些环境变量一般包含敏感和非敏感信息，像 MySQL 的用户名和密码就是敏感信息，主机名和地址算是非敏感信息。

不建议把敏感凭据直接写在 `docker-compose.yml` 中，应放在项目根目录的 `.env` 里，并限制传播范围，降低泄漏风险。Compose 会自动读取同目录下的 `.env`，并用于替换 compose 文件中的 `$VAR` 插值（如 `$MYSQL_USER`）；`env_file: .env` 则把变量**注入容器进程环境**——两者作用不同（参见 [Compose 环境变量](https://docs.docker.com/compose/how-tos/environment-variables/)）。

在项目的根目录 `~/wordpress` 下编辑 `.env` 文件：

```bash
$ vim .env
MYSQL_ROOT_PASSWORD=your_root_password
MYSQL_USER=your_wordpress_database_user
MYSQL_PASSWORD=your_wordpress_database_password
```

该文件包含重要的机密信息，像 MySQL root 密码、WordPress 应用程序要使用到的数据库用户名以及密码。

因为 `.env` 文件包含敏感信息，所以您希望将它包含在项目的 `.gitignore` 和 `.dockerignore` 文件中。这会告诉 Git 和 Docker 什么文件不需要上传到 Git 仓库和 Docker images 仓库。

如果您打算使用 Git 做版本控制，您可能使用 git init 初始化当前工作目录作为本地仓库。

```bash
$ git init
```

然后创建 `.gitignore` 文件，将 `.env` 添加到该文件：

```bash
$ vim .gitignore
.env
```

同样的，在 `.dockerignore` 文件中添加 `.env` 也是一种很好的预防措施，这样当您使用此目录作为构建上下文时，它就不会出现在您的容器中。

```bash
$ vim .dockerignore
.env
```

在下面，您可以添加与应用程序开发相关的文件和目录：

```bash
$ vim .dockerignore
.env
.git
docker-compose.yml
.dockerignore
```

设置好敏感信息之后，接下来就可以在 docker-compose.yml 文件中定义相关服务信息。

## Y5、定义 Docker Compose 服务文件

您的 docker-compose.yml 文件将包含您设置的服务定义。一个 Compose 服务就是一个将要运行的容器，服务定义指明了每个容器将如何运行。

使用 Compose，您可以定义多个不同的服务来运行多个容器应用，因为 Compose 允许您使用共享网络和共享存储卷将这些服务连接在一起。这对于我们接下来要安装多个服务很有用，因为我们要安装 MySQL、WordPress、Nginx 以及 Certbot 4 个容器，并且要将它们组合起来使用。

首先，编辑 docker-compose.yml 文件，添加如下信息：

```bash
$ vim docker-compose.yml
version: '3'

services:
  db:
    image: mysql:8.0
    container_name: db
    restart: unless-stopped
    env_file: .env
    environment:
      - MYSQL_DATABASE=wordpress
    volumes: 
      - dbdata:/var/lib/mysql
    command: '--default-authentication-plugin=mysql_native_password'
    networks:
      - app-network
```

该 db 服务定义包含如下选项：

+ `image`：告诉 Compose 将要拉取什么镜像运行容器，这里指定使用 `mysql:8.0` 镜像，避免使用 `mysql:latest` 将来发生冲突，因为 latest 会持续更新。

  想要获取更多的版本定义信息，以避免依赖冲突，可以阅读官方文档 [Dockerfile best practices](https://docs.docker.com/develop/develop-images/dockerfile_best-practices/)。

+ `container_name`：为容器指定一个名字。

+ `restart`：定义容器的重启策略。默认是 no，您可以设置如果容器停止就重启的策略。

+ `env_file`：该选项告诉 Compose 将要从 `.env` 文件中获取环境变量添加进来，本示例中，`.env` 文件在当前项目根目录。

+ `environment`：该选项允许您添加额外的环境变量，也就是不仅仅可以在 `.env` 文件中定义。您可以把 MYSQL_DATABASE 变量设置为 wordpress，以便为应用程序数据库提供一个名称。因为这是非敏感信息，所以可以直接将其包含在 docker-compose.yml 文件中。

+ `volumes`：该示例会将名为 dbdata 的存储卷挂载到容器的 /var/lib/mysql 目录中，这是 MySQL 大多数发行版的标准数据库目录。

+ `command`：覆盖镜像默认 `CMD`，向 `mysqld` 传入 `--default-authentication-plugin=mysql_native_password`。MySQL 8.0 默认 `caching_sha2_password`，而本文 WordPress/PHP 栈与 `mysql_native_password` 兼容性更好；该选项指定**新账户**的默认插件，已有账户不受影响（参见 [MySQL 身份验证文档](https://dev.mysql.com/doc/refman/8.0/en/authentication-plugins.html)）。

+ `networks`：将容器接入 `app-network`。该网络在文件末尾以 `driver: bridge` 定义，属于 [Docker 用户自定义桥接网络](https://docs.docker.com/engine/network/drivers/bridge/)，同一网络上的容器可通过服务名 DNS 解析互相访问。

接下来，在 db 服务的定义后面，加上 wordpress 应用程序服务的定义：

```bash
...
  wordpress:
    depends_on: 
      - db
    image: wordpress:5.1.1-fpm-alpine
    container_name: wordpress
    restart: unless-stopped
    env_file: .env
    environment:
      - WORDPRESS_DB_HOST=db:3306
      - WORDPRESS_DB_USER=$MYSQL_USER
      - WORDPRESS_DB_PASSWORD=$MYSQL_PASSWORD
      - WORDPRESS_DB_NAME=wordpress
    volumes:
      - wordpress:/var/www/html
    networks:
      - app-network
```

该服务的定义跟 db 服务定义很相似：

+ `depends_on`：声明启动顺序。wordpress 依赖 **db** 服务（Compose 服务名，不是镜像名 `mysql`）。[Compose 文档](https://docs.docker.com/compose/how-tos/startup-order/)说明它只保证 db 容器先启动，**不等待** MySQL 完成初始化；若 WordPress 偶发连库失败，可配合 healthcheck 或应用重试。
+ image：使用 `wordpress:5.1.1-fpm-alpine`。`-fpm` 表示内置 **PHP-FPM**（FastCGI 进程管理器），由 Nginx 通过 FastCGI 协议转发 PHP 请求；`alpine` 基于 [Alpine Linux](https://alpinelinux.org/)，镜像体积更小。
+ `env_file`：从 `.env` 注入数据库凭据等变量。
+ `environment`：映射 WordPress 所需变量。`WORDPRESS_DB_USER`、`WORDPRESS_DB_PASSWORD` 引用 `.env` 中的值；`WORDPRESS_DB_HOST=db:3306` 是 **主机名:端口**（Compose 服务名 `db` 在 `app-network` 内可解析，3306 为 MySQL 默认端口），不是 Unix socket 路径；`WORDPRESS_DB_NAME` 与 db 服务中的 `MYSQL_DATABASE` 一致。
+ `volumes`：这里将取名为 wordpress 的存储卷挂载到由 wordpress 镜像创建的 /var/www/html 挂载点上。以这种方式使用命名存储卷将允许您与其他容器共享应用程序代码。
+ `networks`：同样地，将 wordpress 容器加入到 app-network 网络。

接下来，在 wordpress 服务定义下面加上 nginx webserver 的定义：

```bash
...
  webserver:
    depends_on:
      - wordpress
    image: nginx:1.15.12-alpine
    container_name: webserver
    restart: unless-stopped
    ports:
      - "80:80"
    volumes:
      - wordpress:/var/www/html
      - ./nginx-conf:/etc/nginx/conf.d
      - certbot-etc:/etc/letsencrypt
    networks:
      - app-network
```

通过前面的服务定义学习，很容易理解该服务定义：

+ `ports`：将容器 80 映射到宿主机 80，对外提供 HTTP（ACME 挑战与后续 HTTPS 重定向均依赖此端口可达）。
+ `volumes`：命名卷与绑定挂载组合使用。
  + `wordpress:/var/www/html`：与 WordPress 容器共享站点文件，对应 Nginx `root` 指令路径。
  + `./nginx-conf:/etc/nginx/conf.d`：绑定挂载，宿主机改配置即可生效（需 `nginx -s reload` 或容器重建）。
  + `certbot-etc:/etc/letsencrypt`：与 Certbot 共享证书目录；`live/<域名>/` 下的 `fullchain.pem`、`privkey.pem` 为符号链接，指向 `archive/` 中实际文件（Certbot 标准布局）。

最后，在 webserver 服务的定义下面添加 certbot 服务定义。请务必将这里列出的电子邮件地址和域名替换为您自己的信息。

```bash
certbot:
    depends_on:
      - webserver
    image: certbot/certbot
    container_name: certbot
    volumes:
      - certbot-etc:/etc/letsencrypt
      - wordpress:/var/www/html
    command: certonly --webroot --webroot-path=/var/www/html --email sammy@your_domain --agree-tos --no-eff-email --staging -d your_domain -d www.your_domain
```

该定义从 Docker Hub 拉取 `certbot/certbot` 镜像。Certbot 分两类插件（参见 [Certbot 用户指南](https://eff-certbot.readthedocs.io/en/stable/using.html)）：
+ **Authenticator（验证插件）**：证明域名控制权，本文用 `--webroot`
+ **Installer（安装插件）**：自动修改 Web 服务器配置以启用 HTTPS；本文用 `certonly`，**不**使用 Installer，由你手写 Nginx SSL 配置

命名卷 `certbot-etc` 与 `wordpress` 分别与 Nginx 共享证书目录和 Web 根，使挑战文件可被公网访问、证书可被 Nginx 加载。

`depends_on: webserver` 确保 Nginx 先启动并在 80 端口监听；webroot 模式下若 Web 服务器未运行，HTTP-01 验证无法完成。

`command` 中的 `certonly` 子命令仅**获取或续期证书**，不修改 Nginx。各参数含义：
+ `--webroot`：使用 webroot 验证插件（Authenticator only）
+ `--webroot-path`：Web 根路径，须与 Nginx `root` 及卷挂载一致
+ `--email`：注册 ACME 账户的联系邮箱，用于到期通知等
+ `--agree-tos`：同意 [Let's Encrypt 订阅者协议](https://letsencrypt.org/documents/)
+ `--no-eff-email`：不向 EFF 分享邮箱（Certbot 可选营销邮件）
+ `--staging`：使用 Let's Encrypt **测试** CA，签发不被浏览器信任的测试证书，用于验证配置、避免触发生产环境 [速率限制](https://letsencrypt.org/docs/rate-limits/)
+ `-d`：证书 SAN 中的域名，可多次使用以覆盖多个主机名

在 certbot 服务定义下面，添加您的网络和卷定义：

```bash
...
volumes:
  certbot-etc:
  wordpress:
  dbdata:

networks:
  app-network:
    driver: bridge
```

在全局级别定义命名卷 `certbot-etc`、`wordpress`、`dbdata`。Docker 将卷数据存放在宿主机 `/var/lib/docker/volumes/` 下，由 Docker 管理生命周期；挂载到容器的路径由 Compose `volumes` 映射决定，从而实现跨容器共享。

`app-network` 使用 `bridge` 驱动，创建用户自定义桥接网络（参见 [bridge 网络](https://docs.docker.com/engine/network/drivers/bridge/)）。同一网络上的容器可通过**服务名**（内置 DNS）互相访问任意端口，无需把 db、wordpress 的端口发布到宿主机；仅 webserver 的 80/443 需映射到宿主机供公网访问。

下面是 docker-compose.yml 文件的全部内容：

```bash
version: '3'

services:
  db:
    image: mysql:8.0
    container_name: db
    restart: unless-stopped
    env_file: .env
    environment:
      - MYSQL_DATABASE=wordpress
    volumes: 
      - dbdata:/var/lib/mysql
    command: '--default-authentication-plugin=mysql_native_password'
    networks:
      - app-network

  wordpress:
    depends_on: 
      - db
    image: wordpress:5.1.1-fpm-alpine
    container_name: wordpress
    restart: unless-stopped
    env_file: .env
    environment:
      - WORDPRESS_DB_HOST=db:3306
      - WORDPRESS_DB_USER=$MYSQL_USER
      - WORDPRESS_DB_PASSWORD=$MYSQL_PASSWORD
      - WORDPRESS_DB_NAME=wordpress
    volumes:
      - wordpress:/var/www/html
    networks:
      - app-network

  webserver:
    depends_on:
      - wordpress
    image: nginx:1.15.12-alpine
    container_name: webserver
    restart: unless-stopped
    ports:
      - "80:80"
    volumes:
      - wordpress:/var/www/html
      - ./nginx-conf:/etc/nginx/conf.d
      - certbot-etc:/etc/letsencrypt
    networks:
      - app-network

  certbot:
    depends_on:
      - webserver
    image: certbot/certbot
    container_name: certbot
    volumes:
      - certbot-etc:/etc/letsencrypt
      - wordpress:/var/www/html
    command: certonly --webroot --webroot-path=/var/www/html --email sammy@your_domain --agree-tos --no-eff-email --staging -d your_domain -d www.your_domain

volumes:
  certbot-etc:
  wordpress:
  dbdata:

networks:
  app-network:
    driver: bridge
```

接下来就可以启动容器和测试证书请求。

## Y6、获取 SSL 证书凭证

使用 docker-compose up -d 命令启动所有容器服务，这会按照我们自定义的服务顺序创建和启动各容器。-d 选项是让所有容器在后台运行。

```bash
$ docker-compose up -d
```

下面的输出证明所有的容器服务都启动成功。

```bash
Output
Creating db ... done
Creating wordpress ... done
Creating webserver ... done
Creating certbot   ... done
```

使用 docker-compose ps 检查所有服务的状态。

```bash
$ docker-compose ps
```

一旦完成，您的 db、wordpress 和 webserver 服务都处于 Up 状态，并且 certbot 容器将退出并显示一个 0 状态消息。

```bash
Output
  Name                 Command               State           Ports       
-------------------------------------------------------------------------
certbot     certbot certonly --webroot ...   Exit 0                      
db          docker-entrypoint.sh --def ...   Up       3306/tcp, 33060/tcp
webserver   nginx -g daemon off;             Up       0.0.0.0:80->80/tcp 
wordpress   docker-entrypoint.sh php-fpm     Up       9000/tcp
```

如果不是上面所预期的状态，就需要使用 docker-compose logs 命令查看一下日志信息。

```bash
$ docker-compose logs service_name
```

您可以使用 docker-compose exec 命令查看证书是否已挂载到 webserver 容器中。

```bash
$ docker-compose exec webserver ls -la /etc/letsencrypt/live
```

一旦您的证书请求成功，会输出如下信息。

```bash
Output
total 16
drwx------    3 root     root          4096 May 10 15:45 .
drwxr-xr-x    9 root     root          4096 May 10 15:45 ..
-rw-r--r--    1 root     root           740 May 10 15:45 README
drwxr-xr-x    2 root     root          4096 May 10 15:45 your_domain
```

certbot 为**一次性任务容器**：`certonly` 完成后进程退出，`Exit 0` 表示成功。证书写入共享卷 `certbot-etc`，webserver 通过同一卷在 `/etc/letsencrypt/live/` 读取。

测试证书验证流程无误后，编辑 `docker-compose.yml`，移除 `--staging`，改用 `--force-renewal` 向**生产 CA** 申请正式证书。按 [Certbot 文档](https://eff-certbot.readthedocs.io/en/stable/using.html)，`--force-renewal` 即使现有证书未临近到期也会强制重新申请，成功后更新 `live/` 符号链接指向新证书。

同时增加 `certbot-var:/var/lib/letsencrypt` 卷：存放 ACME 账户密钥、续期配置等元数据（`/etc/letsencrypt` 侧重证书与 `renewal/*.conf`），与仅挂载 `certbot-etc` 的首次定义相比更完整。

以下是更新后的 certbot 服务定义：

```bash
...
  certbot:
    depends_on:
      - webserver
    image: certbot/certbot
    container_name: certbot
    volumes:
      - certbot-etc:/etc/letsencrypt
      - certbot-var:/var/lib/letsencrypt
      - wordpress:/var/www/html
    command: certonly --webroot --webroot-path=/var/www/html --email sammy@your_domain --agree-tos --no-eff-email --force-renewal -d your_domain -d www.your_domain
...
```

您现在可以运行 docker-compose up 来重建 certbot 容器。`--no-deps` 避免重启依赖链上的其他服务；`--force-recreate` 强制用更新后的 `command` 创建新容器。webserver 保持运行即可，HTTP-01 仍由 Nginx 在 80 端口响应。

```bash
$ docker-compose up --force-recreate --no-deps certbot
```

显示如下信息表示证书请求成功：

```bash
Output
Recreating certbot ... done
Attaching to certbot
certbot      | Saving debug log to /var/log/letsencrypt/letsencrypt.log
certbot      | Plugins selected: Authenticator webroot, Installer None
certbot      | Renewing an existing certificate
certbot      | Performing the following challenges:
certbot      | http-01 challenge for your_domain
certbot      | http-01 challenge for www.your_domain
certbot      | Using the webroot path /var/www/html for all unmatched domains.
certbot      | Waiting for verification...
certbot      | Cleaning up challenges
certbot      | IMPORTANT NOTES:
certbot      |  - Congratulations! Your certificate and chain have been saved at:
certbot      |    /etc/letsencrypt/live/your_domain/fullchain.pem
certbot      |    Your key file has been saved at:
certbot      |    /etc/letsencrypt/live/your_domain/privkey.pem
certbot      |    Your cert will expire on 2019-08-08. To obtain a new or tweaked
certbot      |    version of this certificate in the future, simply run certbot
certbot      |    again. To non-interactively renew *all* of your certificates, run
certbot      |    "certbot renew"
certbot      |  - Your account credentials have been saved in your Certbot
certbot      |    configuration directory at /etc/letsencrypt. You should make a
certbot      |    secure backup of this folder now. This configuration directory will
certbot      |    also contain certificates and private keys obtained by Certbot so
certbot      |    making regular backups of this folder is ideal.
certbot      |  - If you like Certbot, please consider supporting our work by:
certbot      | 
certbot      |    Donating to ISRG / Let's Encrypt:   https://letsencrypt.org/donate
certbot      |    Donating to EFF:                    https://eff.org/donate-le
certbot      | 
certbot exited with code 0
```

日志中 `Authenticator webroot, Installer None` 表示仅用 webroot 做域名验证，**未**使用 Installer 自动改 Nginx——与本文手写 SSL 配置的方式一致。

现在证书已就位，接下来就可以配置 nginx 配置文件启用 SSL。

## Y7、修改 nginx 配置启用 SSL

在 Nginx 中启用 HTTPS 需要：保留 80 端口上的 ACME 路径（续期仍用 HTTP-01）；将其他 HTTP 请求重定向到 HTTPS；在 443 上配置 `ssl`、证书与推荐 TLS 参数。

重建 webserver 前可先停止，避免配置切换瞬间的异常响应：

```bash
$ docker-compose stop webserver
```

从 Certbot 官方仓库获取推荐的 Nginx TLS 片段（与 certbot-nginx 插件使用的配置一致）：

```bash
curl -sSLo nginx-conf/options-ssl-nginx.conf https://raw.githubusercontent.com/certbot/certbot/master/certbot-nginx/certbot_nginx/_internal/tls_configs/options-ssl-nginx.conf
```

该文件通常包含 `ssl_protocols`、`ssl_prefer_server_ciphers`、`ssl_session_cache` 等，由 Certbot 维护并与当前最佳实践对齐。

接下来，删除你之前创建的 Nginx 配置文件：

```bash
$ rm nginx-conf/nginx.conf
```

创建并打开文件的另一个版本：

```bash
$ vim nginx-conf/nginx.conf
```

将以下代码添加到文件中，以将 HTTP 重定向到 HTTPS，并添加 SSL 凭据、协议和安全头。记住用你自己的域名替换 your_domain：

```bash
server {
        listen 80;
        listen [::]:80;

        server_name your_domain www.your_domain;

        location ~ /.well-known/acme-challenge {
                allow all;
                root /var/www/html;
        }

        location / {
                rewrite ^ https://$host$request_uri? permanent;
        }
}

server {
        listen 443 ssl http2;
        listen [::]:443 ssl http2;
        server_name your_domain www.your_domain;

        index index.php index.html index.htm;

        root /var/www/html;

        server_tokens off;

        ssl_certificate /etc/letsencrypt/live/your_domain/fullchain.pem;
        ssl_certificate_key /etc/letsencrypt/live/your_domain/privkey.pem;

        include /etc/nginx/conf.d/options-ssl-nginx.conf;

        add_header X-Frame-Options "SAMEORIGIN" always;
        add_header X-XSS-Protection "1; mode=block" always;
        add_header X-Content-Type-Options "nosniff" always;
        add_header Referrer-Policy "no-referrer-when-downgrade" always;
        add_header Content-Security-Policy "default-src * data: 'unsafe-eval' 'unsafe-inline'" always;
        # add_header Strict-Transport-Security "max-age=31536000; includeSubDomains; preload" always;
        # enable strict transport security only if you understand the implications

        location / {
                try_files $uri $uri/ /index.php$is_args$args;
        }

        location ~ \.php$ {
                try_files $uri =404;
                fastcgi_split_path_info ^(.+\.php)(/.+)$;
                fastcgi_pass wordpress:9000;
                fastcgi_index index.php;
                include fastcgi_params;
                fastcgi_param SCRIPT_FILENAME $document_root$fastcgi_script_name;
                fastcgi_param PATH_INFO $fastcgi_path_info;
        }

        location ~ /\.ht {
                deny all;
        }
        
        location = /favicon.ico { 
                log_not_found off; access_log off; 
        }
        location = /robots.txt { 
                log_not_found off; access_log off; allow all; 
        }
        location ~* \.(css|gif|ico|jpeg|jpg|js|png)$ {
                expires max;
                log_not_found off;
        }
}
```

这里的 HTTP `server` 块保留 `/.well-known/acme-challenge`（证书续期仍依赖 HTTP-01）；`location /` 中的 `rewrite` 将其余 HTTP 请求 **301 永久重定向**到 HTTPS（参见 [rewrite](http://nginx.org/en/docs/http/ngx_http_rewrite_module.html#rewrite)）。替换串末尾的 `?` 表示不再追加原 `$args`（因 `$request_uri` 已含完整路径与查询串）。

HTTPS `server` 块要点：
+ `listen 443 ssl http2`：在 443 启用 SSL/TLS；`http2` 作为 `listen` 参数在 nginx 1.25.1+ 已弃用，应改用独立 `http2 on` 指令；本文使用 nginx 1.15.12，此写法正确且与当时官方示例一致。
+ `ssl_certificate` / `ssl_certificate_key`：分别指向**证书链**与**私钥** PEM 文件（参见 [ngx_http_ssl_module](http://nginx.org/en/docs/http/ngx_http_ssl_module.html)）。`fullchain.pem` 含叶子证书及中间证书（叶子在前）；`privkey.pem` 仅含私钥，须限制权限且 nginx master 进程可读。
+ `include options-ssl-nginx.conf`：引入 Certbot 维护的 TLS 参数片段。
+ `server_tokens off`：错误页不暴露 nginx 版本号。

以下 `add_header` 为应用层安全响应头，与 TLS 配置互补，可用于 [SSL Labs](https://www.ssllabs.com/ssltest/)、[Security Headers](https://securityheaders.com/) 等检测：
+ [`X-Frame-Options`](https://developer.mozilla.org/en-US/docs/Web/HTTP/Headers/X-Frame-Options)：限制页面是否可被嵌入 iframe
+ [`X-Content-Type-Options`](https://developer.mozilla.org/en-US/docs/Web/HTTP/Headers/X-Content-Type-Options)：禁止 MIME 嗅探
+ [`Referrer-Policy`](https://developer.mozilla.org/en-US/docs/Web/HTTP/Headers/Referrer-Policy)：控制 Referer 泄露范围
+ [`Content-Security-Policy`](https://developer.mozilla.org/en-US/docs/Web/HTTP/Headers/Content-Security-Policy)：限制资源加载来源（本文为 WordPress 兼容的宽松策略）
+ [`X-XSS-Protection`](https://developer.mozilla.org/en-US/docs/Web/HTTP/Headers/X-XSS-Protection)：旧版浏览器 XSS 过滤提示，现代浏览器已逐步弃用，保留不影响主流浏览器

[HSTS](https://developer.mozilla.org/en-US/docs/Web/HTTP/Headers/Strict-Transport-Security)（`Strict-Transport-Security`）已注释。启用后浏览器在 `max-age` 内强制 HTTPS；`includeSubDomains`、`preload` 影响子域及预加载列表，误配可能导致长时间无法通过 HTTP 访问，理解影响后再启用。

在重建 webserver 服务之前，您需要添加一个 443 映射端口到您 webserver 服务定义中。

打开 docker-compose.yml 文件：

```bash
$ vim docker-compose.yml
```

在 webserver 服务定义中添加如下端口映射：

```bash
...
  webserver:
    depends_on:
      - wordpress
    image: nginx:1.15.12-alpine
    container_name: webserver
    restart: unless-stopped
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - wordpress:/var/www/html
      - ./nginx-conf:/etc/nginx/conf.d
      - certbot-etc:/etc/letsencrypt
    networks:
      - app-network
```

下面是完整的 docker-compose.yml 文件内容：

```bash
version: '3'

services:
  db:
    image: mysql:8.0
    container_name: db
    restart: unless-stopped
    env_file: .env
    environment:
      - MYSQL_DATABASE=wordpress
    volumes: 
      - dbdata:/var/lib/mysql
    command: '--default-authentication-plugin=mysql_native_password'
    networks:
      - app-network

  wordpress:
    depends_on: 
      - db
    image: wordpress:5.1.1-fpm-alpine
    container_name: wordpress
    restart: unless-stopped
    env_file: .env
    environment:
      - WORDPRESS_DB_HOST=db:3306
      - WORDPRESS_DB_USER=$MYSQL_USER
      - WORDPRESS_DB_PASSWORD=$MYSQL_PASSWORD
      - WORDPRESS_DB_NAME=wordpress
    volumes:
      - wordpress:/var/www/html
    networks:
      - app-network

  webserver:
    depends_on:
      - wordpress
    image: nginx:1.15.12-alpine
    container_name: webserver
    restart: unless-stopped
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - wordpress:/var/www/html
      - ./nginx-conf:/etc/nginx/conf.d
      - certbot-etc:/etc/letsencrypt
    networks:
      - app-network

  certbot:
    depends_on:
      - webserver
    image: certbot/certbot
    container_name: certbot
    volumes:
      - certbot-etc:/etc/letsencrypt
      - wordpress:/var/www/html
    command: certonly --webroot --webroot-path=/var/www/html --email sammy@your_domain --agree-tos --no-eff-email --force-renewal -d your_domain -d www.your_domain

volumes:
  certbot-etc:
  wordpress:
  dbdata:

networks:
  app-network:
    driver: bridge
```

重建 webserver 服务：

```bash
$ docker-compose up -d --force-recreate --no-deps webserver
```

使用 docker-compose ps 查看服务：

```bash
$ docker-compose ps
Output
  Name                 Command               State                     Ports                  
----------------------------------------------------------------------------------------------
certbot     certbot certonly --webroot ...   Exit 0                                           
db          docker-entrypoint.sh --def ...   Up       3306/tcp, 33060/tcp                     
webserver   nginx -g daemon off;             Up       0.0.0.0:443->443/tcp, 0.0.0.0:80->80/tcp
wordpress   docker-entrypoint.sh php-fpm     Up       9000/tcp 
```

接下来可以通过 web 接口来配置 WordPress。

## Y8、通过 Web 接口完成安装

容器运行后，通过 WordPress web 界面完成安装，打开浏览器输入 https://your_domain。根据提示完成 wordpress 的安装。接着就可以使用 wordpress。

## Y9、更新证书

Let's Encrypt 证书有效期 90 天。应配置自动续期，在到期前由 `certbot renew` 更新证书，并**重载 Nginx** 以加载新证书文件（证书路径不变，但磁盘内容已更新）。

编辑 ssl_renew.sh 脚本文件：

```bash
$ vim ssl_renew.sh
```

向脚本中添加以下代码以更新证书并重新加载 web 服务器配置。记住用你自己的非 root 用户名替换这里的示例用户名：

```bash
#!/bin/bash

COMPOSE="/usr/local/bin/docker-compose --ansi never"
DOCKER="/usr/bin/docker"

cd /home/sammy/wordpress/
$COMPOSE run certbot renew --dry-run && $COMPOSE kill -s SIGHUP webserver
$DOCKER system prune -af
```

该脚本设置 Compose 与 Docker 可执行文件路径；`--ansi never`（旧版 Compose 为 `--no-ansi`）避免 cron 邮件中出现 [ANSI 控制字符](https://vt100.net/docs/vt510-rm/chapter4.html)。`cd` 进入项目目录后执行：

+ `docker-compose run certbot renew`：`run` 启动一次性 certbot 容器并覆盖服务定义中的 `command`，执行 `renew` 子命令。按 [Certbot 文档](https://eff-certbot.readthedocs.io/en/stable/using.html)，`renew` **仅处理临近到期的证书**（Certbot 4.0+ 阈值为剩余寿命不足 1/3；更短寿命证书为 1/2；旧版为固定 30 天）。未到期时通常无操作，故可频繁调度。`--dry-run` 走完整流程但不保存证书，用于验证续期配置。
+ [`docker-compose kill -s SIGHUP`](https://docs.docker.com/engine/reference/commandline/compose_kill/)：向 webserver 主进程发送 **SIGHUP**，触发 nginx **优雅重载**配置与证书（参见 [nginx 控制](http://nginx.org/en/docs/control.html)），无需停容器。续期后必须重载，否则 nginx 可能仍使用旧证书。
+ [`docker system prune -af`](https://docs.docker.com/engine/reference/commandline/system_prune/)：清理未使用的容器、网络与镜像，释放磁盘（生产环境可按需调整频率）。

给该脚本一个执行权限：

```bash
$ chmod +x ssl_renew.sh
```

接下来，打开 root crontab 文件，设置时钟计划来运行 renewal 脚本：

```bash
$ sudo crontab -e 
```

在这个文件的最底部，添加以下一行：

```bash
*/5 * * * * /home/sammy/wordpress/ssl_renew.sh >> /var/log/cron.log 2>&1
```

该任务计划设置为每五分钟执行一次，因此您可以测试您的更新请求是否按预期工作。创建一个日志文件 cron.log 来记录任务计划的相关输出。

等待 5 分钟后，检查 cron.log 来确认更新证书请求是否成功。

```bash
$ tail -f /var/log/cron.log
```

以下输出确认更新成功：

```bash
Output
- - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -
** DRY RUN: simulating 'certbot renew' close to cert expiry
**          (The test certificates below have not been saved.)

Congratulations, all renewals succeeded. The following certs have been renewed:
  /etc/letsencrypt/live/your_domain/fullchain.pem (success)
** DRY RUN: simulating 'certbot renew' close to cert expiry
**          (The test certificates above have not been saved.)
- - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -
```

> 注意：若 certbot 容器仍在运行，执行脚本可能出现 container name already in use。certbot 设计为任务型容器，证书操作完成后应已退出；若未退出，用 `docker-compose rm -f certbot` 清理后再跑续期。

验证通过后移除 `--dry-run`，执行真实续期：

```bash
#!/bin/bash

COMPOSE="/usr/local/bin/docker-compose --ansi never"
DOCKER="/usr/bin/docker"

cd /home/sammy/wordpress/
$COMPOSE run certbot renew && $COMPOSE kill -s SIGHUP webserver
$DOCKER system prune -af
```

证书 90 天才过期，cron 无需每 5 分钟执行；[Certbot 建议](https://eff-certbot.readthedocs.io/en/stable/using.html)每天运行两次 `renew` 即可（未到期时几乎无操作）。下面改为每天 08:08 执行：

```bash
8 8 * * * /home/sammy/wordpress/ssl_renew.sh >> /var/log/cron.log 2>&1
```

