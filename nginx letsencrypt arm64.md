## X1、简介

WordPress 是一个免费的、开源的内容管理系统(CMS)，建立在 MySQL 数据库和 PHP 语言能力的基础上。由于其可扩展的插件架构和模板系统，它的大部分管理可以通过 web 界面完成。

这就是为什么在创建不同类型的网站时，从博客到产品页面到电子商务网站，WordPress 是一个受欢迎的选择。

搭建 WordPress 通常包括安装 LAMP(Linux, Apache, MySQL, PHP) 或者 LNMP(Linux, Nginx, MySQL, PHP) 等相关技术栈，需要一定的基础能力。

当然，我们下面使用 Docker 和 Docker Compose 来实现 WordPress 的搭建很方便，至于 Docker 是什么，这是一个当今流行且很大的话题。需要花一些时间学习，这里直接上手使用。

接下来的教程中，我们将会安装 MySQL、Nginx、WordPress 以及 Certbot 4 个容器。Certbot 这个容器是用来给 WordPress 关联的域名申请 TLS/SSL 证书的，也就是实现 https 协议访问 WordPress 网站。

申请证书的机构是 [Let's Encrypt](https://letsencrypt.org/zh-cn/) 这个非盈利性证书颁发机构。每次申请的证书有效期是 3 个月，到期后需要重新申请，所以我们需要设置一个 cronjob 来 renew 证书，以保证我们域名的安全。

## X2、前提条件

为了实现上面我们所说的目的，需要有下面这些前提条件：

+ 操作系统 Ubuntu 20.04 或者 CentOS 8.4，要求有 root 权限和防火墙服务(iptables.service)。
+ 安装 Docker 引擎，参见[官方手册](https://docs.docker.com/engine/install/centos/)。
+ 安装 Docker Compose，安装 Compose 插件或者独立的二进制包，参见[官方手册](https://docs.docker.com/compose/install/linux/)。
+ 注册一个域名，本文通篇会使用该域名。比如本人的域名是 brinnatt.com。
+ 给注册好的域名 brinnatt.com 添加如下两条 A 记录。
  + 使用 brinnatt.com 添加一条 A 记录指向我们的服务器公网地址。
  + 使用 www.brinnatt.com 添加一条 A 记录指向我们的服务器公网地址。

一切准备就绪后，就可以开始我们的部署行动。

## X3、定义 Web 服务器配置

在开始运行任何容器之前，首先就要定义好 Nginx Web 服务器的配置。该配置包括 WordPress 相关的代码块，还包括 Let's Encrypt 相关的代码块，用于将 Let 's Encrypt 验证请求直接发送到 Certbot 客户端，以实现自动证书更新。

首先为 WordPress 创建一个专门的项目根目录，比如：

```bash
[root@arm2 ~]# mkdir /usr/local/wordpress
```

然后，进入 wordpress 项目根目录：

```bash
[root@arm2 ~]# cd /usr/local/wordpress
```

接着，为 nginx 配置文件创建一个目录：

```bash
[root@arm2 wordpress]# mkdir nginx-conf
```

最后，编辑配置文件，在这个文件里添加 server 代码块，配置好 server_name 指令、文档根目录、以及其它代码块，用以处理 Certbot 客户端的证书请求，PHP 动态网页和静态资源请求等。

```bash
[root@arm2 wordpress]# vim nginx-conf/nginx.conf
server {
        listen 80;
        listen [::]:80;

        server_name brinnatt.com www.brinnatt.com;

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

+ `listen`：该指令告诉 nginx 监听在 80 端口，这将允许你使用 Certbot 的 webroot 插件来处理你的证书请求。注意，现在还没有包括端口 443。成功获得证书后，就可以更新配置以包括 SSL。

+ `server_name`：该指令定义多个主机名，第一个名字为虚拟主机的首要主机名。也就是响应用户请求的主机。请将 your_domain 替换成自己的域名。

+ `index`：该指令定义响应客户请求的索引文件。该索引文件有前后顺序，优先响应前面的索引文件，如果不存在，搜索后面的文件。

+ `root`：该指令定义响应客户请求的 root 根目录。`/var/www/html` 这个目录是根据 WordPress Dockerfile 中的指令在构建时作为挂载点创建的。这些 Dockerfile 指令还确保来自 WordPress 发行版的文件被挂载到这个卷上。

+ `location ~ /.well-known/acme-challenge`：该代码块将处理对 `.well-known` 目录的请求，Certbot 会在里面放置一个临时文件，用来验证可以通过 DNS 将您的域名解析到该服务器。有了该配置，您就可以使用 Certbot 的 webroot 插件来为您的域名获取证书。

+ `location /`：在该代码块中，没有精确匹配的 location 都将匹配该代码块。try_files 指令工作流程如下。
  + `$uri`：首先尝试直接访问请求的文件。
    + 例如请求 `/about.html` → 查找 `/var/www/html/about.html`
  + `$uri/`：如果上一步没找到，尝试作为目录访问
    + 例如请求 `/blog/` → 查找 `/var/www/html/blog/index.php` (受 index 指令影响)
  + `/index.php$is_args$args` - 如果前两步都失败，将请求转发给 index.php
    + `$is_args`：如果原始请求有参数 `?xxx` 则添加 `?`，否则为空
    + `$args`：保留原始请求参数
    + 例如请求 `/non-existent-page` → 内部转发到 `/index.php?non-existent-page`
+ `location ~ \.php$`：该代码块将会处理 PHP 动态请求，并代理这些请求给 WordPress 容器。因为您的 WordPress 容器镜像是基于 `php:fpm` 镜像，您还将在此代码块中包含特定于 FastCGI 协议的配置选项。Nginx 需要一个独立的 PHP 处理器来处理 PHP 请求。在本示例中，这些请求将由 php:fpm 镜像中的 php-fpm 处理器进行处理。
  + `try_files $uri =404`：首先检查请求的 PHP 文件是否存在，如果不存在直接返回 404，防止任意代码执行漏洞。
  + `fastcgi_split_path_info ^(.+\.php)(/.+)$`：正则表达式 `^(.+\.php)(/.+)$` 将请求路径分成两部分。
    + `$fastcgi_script_name`：PHP 脚本路径 (如 /script.php)
    + `$fastcgi_path_info`：路径信息 (如 /additional/path)
  + `fastcgi_pass wordpress:9000`：指定 PHP-FPM 服务地址，这里使用 Docker 服务名 wordpress 和端口 9000
  + `fastcgi_index index.php`：当请求指向目录时使用的默认 PHP 文件
  + `include fastcgi_params`：包含 FastCGI 的标准参数文件
  + 自定义 FastCGI 参数
    + `SCRIPT_FILENAME`：告诉 PHP-FPM 要执行的文件完整路径
    + `PATH_INFO`：传递路径信息给 PHP 脚本

  另外，该代码块还包括 FastCGI 特定的指令、变量以及将代理请求给 WordPress 容器应用的选项，为解析过的请求 URI 设置首选索引，并解析 URI 请求。

+ `location ~ /\.ht`：该代码块将处理 `.htaccess` 文件，因为 Nginx 不会为它们提供服务。`deny_all` 指令保证这些 `.htaccess` 文件永远不会服务给用户。

+ `location = /favicon.ico`，`location = /robots.txt`：这些代码块保证对 `/favicon.ico` 和 `/robots.txt` 的请求不会被记录进日志。

+ `location ~* \.(css|gif|ico|jpeg|jpg|js|png)$`：该代码块关闭了对静态资产请求的日志记录，并确保这些资产是高度可缓存的，因为通常它们提供服务的代价很大。

Nginx 配置就绪后，就可以继续创建环境变量，以便在运行时传递给应用程序和数据库。

## X4、定义环境变量

您的数据库和 WordPress 容器应用程序需要在运行时访问某些环境变量，以便让应用程序的数据可以持久化，并随时可以被应用程序访问到。

要设置的这些环境变量一般包含敏感和非敏感信息，像 MySQL 的用户名和密码就是敏感信息，主机名和地址算是非敏感信息。

不建议在 Docker Compose 文件中设置这些重要的环境变量，而是在 `.env` 文件中设置敏感值并限制其流通。可以有效避免重要的信息泄漏出去。

在项目的根目录 `~/wordpress` 下编辑 `.env` 文件：

```bash
[root@arm2 wordpress]# vim .env
MYSQL_ROOT_PASSWORD=WelC0me168!
MYSQL_USER=wordpress
MYSQL_PASSWORD=WelC0me168!
```

该文件包含重要的机密信息，像 MySQL root 密码、WordPress 应用程序要使用到的数据库用户名以及密码。

因为 `.env` 文件包含敏感信息，所以您希望将它包含在项目的 `.gitignore` 和 `.dockerignore` 文件中。这会告诉 Git 和 Docker 什么文件不需要上传到 Git 仓库和 Docker images 仓库。

如果您打算使用 Git 做版本控制，您可能使用 git init 初始化当前工作目录作为本地仓库。

```bash
[root@arm2 wordpress]# git init
hint: Using 'master' as the name for the initial branch. This default branch name
hint: is subject to change. To configure the initial branch name to use in all
hint: of your new repositories, which will suppress this warning, call:
hint: 
hint: 	git config --global init.defaultBranch <name>
hint: 
hint: Names commonly chosen instead of 'master' are 'main', 'trunk' and
hint: 'development'. The just-created branch can be renamed via this command:
hint: 
hint: 	git branch -m <name>
Initialized empty Git repository in /usr/local/wordpress/.git/
```

然后创建 `.gitignore` 文件，将 `.env` 添加到该文件：

```bash
[root@arm2 wordpress]# vim .gitignore
.env
```

同样的，在 `.dockerignore` 文件中添加 `.env` 也是一种很好的预防措施，这样当您使用此目录作为构建上下文时，它就不会出现在您的容器中。

```bash
[root@arm2 wordpress]# vim .dockerignore
.env
```

在下面，您可以添加与应用程序开发相关的文件和目录：

```bash
[root@arm2 wordpress]# vim .dockerignore 
.env
.git
docker-compose.yml
.dockerignore
```

设置好敏感信息之后，接下来就可以在 docker-compose.yml 文件中定义相关服务信息。

## X5、定义 Docker Compose 服务文件

您的 docker-compose.yml 文件将包含您设置的服务定义。一个 Compose 服务就是一个将要运行的容器，服务定义指明了每个容器将如何运行。

使用 Compose，您可以定义多个不同的服务来运行多个容器应用，因为 Compose 允许您使用共享网络和共享存储卷将这些服务连接在一起。这对于我们接下来要安装多个服务很有用，因为我们要安装 MySQL、WordPress、Nginx 以及 Certbot 4 个容器，并且要将它们组合起来使用。

首先，编辑 docker-compose.yml 文件，添加如下信息：

```bash
[root@arm2 wordpress]# vim docker-compose.yml
services:
  db:
    image: mysql:8.0.31
    container_name: db
    command: '--default-authentication-plugin=mysql_native_password'
    volumes:
      - dbdata:/var/lib/mysql
    restart: unless-stopped
    env_file: .env
    environment:
      - MYSQL_DATABASE=wordpress
    networks:
      - app-network
```

该 db 服务定义包含如下选项：

+ `image`：告诉 Compose 将要拉取什么镜像运行容器，这里指定使用 `mysql:8.0.31` 镜像，避免使用 `mysql:latest` 将来发生冲突，因为 latest 会持续更新。

  想要获取更多的版本定义信息，以避免依赖冲突，可以阅读官方文档 [Dockerfile best practices](https://docs.docker.com/develop/develop-images/dockerfile_best-practices/)。

+ `container_name`：为容器指定一个名字。

+ `restart`：定义容器的重启策略。默认是 no，您可以设置如果容器停止就重启的策略。

+ `env_file`：该选项告诉 Compose 将要从 `.env` 文件中获取环境变量添加进来，本示例中，`.env` 文件在当前项目根目录。

+ `environment`：该选项允许您添加额外的环境变量，也就是不仅仅可以在 `.env` 文件中定义。您可以把 MYSQL_DATABASE 变量设置为 wordpress，以便为应用程序数据库提供一个名称。因为这是非敏感信息，所以可以直接将其包含在 docker-compose.yml 文件中。

+ `volumes`：该示例会将名为 dbdata 的存储卷挂载到容器的 /var/lib/mysql 目录中，这是 MySQL 大多数发行版的标准数据库目录。

+ `command`：该命令会覆盖镜像中默认的 CMD 指令。在某些特殊场景下，您需要为 Docker 镜像的标准 mysqld 命令添加一个选项，以便在容器中启动 MySQL 服务器。这里设置 default-authentication-plugin 系统变量为 mysql_native_password，指定应该使用哪种身份验证机制管理服务器的新身份验证请求。

  因为 PHP 和 WordPress 镜像不支持 MySQL 新版本默认身份验证，所以必须进行此调整，以便对应用程序数据库用户进行身份验证。

+ `networks`：该指令告诉应用程序服务器自己将要添加到哪个网络，该示例是添加到 app-network 二层桥网络中。注意，app-network 二层桥需要定义在文件的全局。

接下来，在 db 服务的定义后面，加上 wordpress 应用程序服务的定义：

```bash
...
  wordpress:
    depends_on: 
      - db
    image: wordpress:6.1.0-fpm-alpine
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

+ `depends_on`：定义依赖关系，该示例定义 wordpress 服务依赖于 mysql 服务，因为 wordpress 的数据要保存到 mysql 数据库中，所示 mysql 服务就绪后，启动 wordpress 容器服务才有意义。
+ image：该示例将使用 `wordpress:6.1.0-fpm-alpine` 镜像，正如我们前面所说的，php-fpm 处理器专门用来处理 PHP 动态请求；还有一个 alpine 镜像，该镜像来自于 [Alpine Linux project](https://alpinelinux.org/)，这将有助于控制整体镜像的大小。
+ `env_file`：同样的，我们需要使用该指令获取 `.env` 文件中的环境变量，因为里面定义了应用程序数据库的用户名和密码。
+ `environment`：我们使用的是在 `.env` 文件中定义的值，但是需要将这些值分配给 WordPress 镜像所期望的变量名：`WORDPRESS_DB_USER` 和 `WORDPRESS_DB_PASSWORD`。另外，我们还要额外定义 `WORDPRESS_DB_HOST` 变量，这是在 db 容器中运行的 MySQL 服务器监听的套接字，默认端口是 3306。`WORDPRESS_DB_NAME` 变量是指明 wordpress 应用程序将要使用到的数据库名，跟 db 服务定义中的 `MYSQL_DATABASE` 变量名一致。
+ `volumes`：这里将取名为 wordpress 的存储卷挂载到由 wordpress 镜像创建的 /var/www/html 挂载点上。以这种方式使用命名存储卷将允许您与其他容器共享应用程序代码。
+ `networks`：同样地，将 wordpress 容器加入到 app-network 网络。

接下来，在 wordpress 服务定义下面加上 nginx webserver 的定义：

```bash
...
  webserver:
    depends_on:
      - wordpress
    image: nginx:1.22.1-alpine
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

+ ports：该容器服务将暴露 80 端口，启用在 nginx.conf 文件中定义的配置选项。
+ `volumes`：该示例将结合使用命名存储卷和绑定挂载。
  + `wordpress:/var/www/html`：该示例将把你的 WordPress 应用程序代码挂载到 nginx 容器的 /var/www/html 目录下，这个目录是你在 Nginx 服务器代码块中设置的根目录。
  + `./nginx-conf:/etc/nginx/conf.d`：这将绑定宿主机上的 Nginx 配置目录挂载到容器上的相关目录，确保您对宿主机上的文件所做的任何更改都将反映到容器中。
  + `certbot-etc:/etc/letsencrypt`：这将把您域名相关的 Let 's Encrypt 证书和密钥挂载到容器上的适当目录。

最后，在 webserver 服务的定义下面添加 certbot 服务定义。请务必将这里列出的电子邮件地址和域名替换为您自己的信息。

```bash
...  
  certbot:
    depends_on:
      - webserver
    image: certbot/certbot:arm64v8-v1.32.0
    container_name: certbot
    volumes:
      - certbot-etc:/etc/letsencrypt
      - wordpress:/var/www/html
    command: certonly --webroot --webroot-path=/var/www/html --email brinnatt@gmail.com --agree-tos --no-eff-email --staging -d brinnatt.com -d www.brinnatt.com
```

该定义告诉 Compose 从 Docker Hub 上拉取 certbot/certbot 镜像运行容器。这里同样用命名存储卷，并且与 nginx 容器共享资源，包括 certbot-etc 中的域名证书和密钥以及 wordpress 容器中的应用程序代码。

certbot 容器依赖 webserver 是必须的，如果 nginx 都没有启动，更新证书没有意义。

这里还包含了一个命令选项，它指定一个要与容器的默认 certbot 命令一起运行的子命令。certonly 子命令将获得一个具有以下选项的证书：

+ `--webroot`：这里告诉 Certbot 使用 webroot 插件将文件放在 webroot 文件夹中进行身份验证。该插件依赖于 [HTTP-01 验证方法](https://datatracker.ietf.org/doc/html/draft-ietf-acme-acme-03#section-7.2)，该方法使用 HTTP 请求来证明 Certbot 可以从响应给定域名的服务器访问资源。
+ `--webroot-path`：这里指定 webroot 目录的路径。
+ `--email`：指定首选邮箱用于注册和恢复。
+ `--agree-tos`：这里指定您同意 [ACME’s Subscriber Agreement](https://letsencrypt.org/documents/LE-SA-v1.2-November-15-2017.pdf)。
+ `--no-eff-email`：这里告诉 Certbot 您不希望共享电子邮件给 [Electronic Frontier Foundation](https://www.eff.org/) (EFF)。
+ `--staging`：这里告诉 Certbot，您希望使用 Let 's Encrypt 的 staging 环境来获得测试证书。使用此选项允许您测试配置选项并避免可能的域请求限制。有关这些限制的更多信息，请阅读 Let’s Encrypt’s [rate limits documentation](https://letsencrypt.org/docs/rate-limits/)。
+ `-d`：这允许您指定多个应用于请求证书的域名。在本例中，包含了 brinnatt.com 和 www.brinnatt.com。请确保将它们替换为您自己的域名。

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

在全局级别使用 volumes 关键字定义存储卷 certbot-etc、wordpress 和 dbdata。当 Docker 创建卷时，卷的内容存储在宿主机文件系统的 /var/lib/docker/volumes/ 目录中，该目录由 Docker 管理。然后将每个卷的内容从这个目录挂载到使用该卷的任何容器。通过这种方式，可以在容器之间共享代码和数据。

用户自定义的桥接网络 app-network 支持容器之间的通信，因为它们位于相同的 Docker 守护进程主机上。这简化了应用程序内的通信，因为它打开了同一桥接网络上容器之间的所有端口，而不向外界暴露任何端口。

因此，您的 db、wordpress 和 webserver 容器可以相互通信，您只需要公开端口 80 来进行应用程序的前端访问。

下面是 docker-compose.yml 文件的全部内容：

```bash
version: '3'

services:
  db:
    image: mysql:8.0.31
    container_name: db
    command: '--default-authentication-plugin=mysql_native_password'
    volumes:
      - dbdata:/var/lib/mysql
    restart: unless-stopped
    env_file: .env
    environment:
      - MYSQL_DATABASE=wordpress
    networks:
      - app-network
  wordpress:
    depends_on: 
      - db
    image: wordpress:6.1.0-fpm-alpine
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
    image: nginx:1.22.1-alpine
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
    image: certbot/certbot:arm64v8-v1.32.0
    container_name: certbot
    volumes:
      - certbot-etc:/etc/letsencrypt
      - wordpress:/var/www/html
    command: certonly --webroot --webroot-path=/var/www/html --email brinnatt@gmail.com --agree-tos --no-eff-email --staging -d brinnatt.com -d www.brinnatt.com
volumes:
  certbot-etc:
  wordpress:
  dbdata:

networks:
  app-network:
    driver: bridge
```

接下来就可以启动容器和测试证书请求。

## X6、获取 SSL 证书凭证

使用 docker-compose up -d 命令启动所有容器服务，这会按照我们自定义的服务顺序创建和启动各容器。-d 选项是让所有容器在后台运行。

```bash
[root@arm2 wordpress]# docker-compose up -d
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
[root@arm2 wordpress]# docker-compose ps
```

一但完成，您的 db、wordpress 和 webserver 服务都处于 Up 状态，并且 certbot 容器将退出并显示一个 0 状态消息。

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
[root@arm2 wordpress]# docker-compose logs certbot
```

您可以使用 docker-compose exec 命令查看证书是否已挂载到 webserver 容器中。

```bash
[root@arm2 wordpress]# docker-compose exec webserver ls -la /etc/letsencrypt/live
```

一旦您的证书请求成功，会输出如下信息。

```bash
Output
total 4
drwx------    3 root     root            40 Feb 28 14:45 .
drwxr-xr-x    9 root     root           108 Feb 28 14:47 ..
-rw-r--r--    1 root     root           740 Feb 28 14:45 README
drwxr-xr-x    2 root     root            93 Feb 28 14:45 brinnatt.com
```

现在，您已经知道您的证书请求是成功的，您可以编辑 certbot 服务定义，移除 --staging 标识。

打开 docker-compose.yml 文件：

```bash
[root@arm2 wordpress]# vim docker-compose.yml
```

找到 certbot 服务定义，将命令行中 --staging 标识替换成 --force-renewal 标识，它会告诉 Certbot，您想请求一个与现有证书具有相同域名的新证书。

以下是更新后的 certbot 服务定义：

```bash
...
  certbot:
    depends_on:
      - webserver
    image: certbot/certbot:arm64v8-v1.32.0
    container_name: certbot
    volumes:
      - certbot-etc:/etc/letsencrypt
      - wordpress:/var/www/html
    command: certonly --webroot --webroot-path=/var/www/html --email brinnatt@gmail.com --agree-tos --no-eff-email --force-renewal -d brinnatt.com -d www.brinnatt.com
...
```

您现在可以运行 docker-compose up 来重建 certbot 容器。您需要使用 --no-deps 选项来告诉 Compose 不需要重启 webserver 容器服务，因为 webserver 处理正常运行状态。

```bash
[root@arm2 wordpress]# docker-compose up --force-recreate --no-deps certbot
```

显示如下信息表示证书请求成功：

```bash
Output
[+] Running 1/0
 ⠿ Container certbot  Recreated                                                         0.0s
Attaching to certbot
certbot  | Saving debug log to /var/log/letsencrypt/letsencrypt.log
certbot  | Account registered.
certbot  | Renewing an existing certificate for brinnatt.com and www.brinnatt.com
certbot  | 
certbot  | Successfully received certificate.
certbot  | Certificate is saved at: /etc/letsencrypt/live/brinnatt.com/fullchain.pem
certbot  | Key is saved at:         /etc/letsencrypt/live/brinnatt.com/privkey.pem
certbot  | This certificate expires on 2023-05-29.
certbot  | These files will be updated when the certificate renews.
certbot  | NEXT STEPS:
certbot  | - The certificate will need to be renewed before it expires. Certbot can automatically renew the certificate in the background, but you may need to take steps to enable that functionality. See https://certbot.org/renewal-setup for instructions.
certbot  | 
certbot  | - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -
certbot  | If you like Certbot, please consider supporting our work by:
certbot  |  * Donating to ISRG / Let's Encrypt:   https://letsencrypt.org/donate
certbot  |  * Donating to EFF:                    https://eff.org/donate-le
certbot  | - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -
certbot exited with code 0
```

现在证书已就位，接下来就可以配置 nginx 配置文件启用 SSL。

## X7、修改 nginx 配置启用 SSL

在 Nginx 配置中启用 SSL 需要添加一个 HTTP 重定向到 HTTPS，指定 SSL 证书和密钥位置，并添加安全参数和报头。

因为你要重新创建 webserver 服务来包含这些添加项，你现在可以停止它：

```bash
[root@arm2 wordpress]# docker-compose stop webserver
```

在修改配置文件之前，使用 curl 从 Certbot 中获取推荐的 Nginx 安全参数：

```bash
curl -sSLo nginx-conf/options-ssl-nginx.conf https://raw.githubusercontent.com/certbot/certbot/master/certbot-nginx/certbot_nginx/_internal/tls_configs/options-ssl-nginx.conf
```

这个命令将把这些参数保存在 nginx-conf 目录下的一个名为 options-ssl-nginx.conf 的文件中。

接下来，删除你之前创建的 Nginx 配置文件：

```bash
[root@arm2 wordpress]# rm nginx-conf/nginx.conf 
rm: remove regular file 'nginx-conf/nginx.conf'? y
```

创建并打开文件的另一个版本：

```bash
[root@arm2 wordpress]# vim nginx-conf/nginx.conf
```

将以下代码添加到文件中，以将 HTTP 重定向到 HTTPS，并添加 SSL 凭据、协议和安全头。记住用你自己的域名替换 brinnatt.com：

```bash
server {
        listen 80;
        listen [::]:80;

        server_name brinnatt.com www.brinnatt.com;

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
        server_name brinnatt.com www.brinnatt.com;

        index index.php index.html index.htm;

        root /var/www/html;

        server_tokens off;

        ssl_certificate /etc/letsencrypt/live/brinnatt.com/fullchain.pem;
        ssl_certificate_key /etc/letsencrypt/live/brinnatt.com/privkey.pem;

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

这里的 HTTP server 代码块为 Certbot 更新请求到 `.well-known/acme-challenge` 目录指定 webroot，还包括一个重写指令 [rewrite directive](http://nginx.org/en/docs/http/ngx_http_rewrite_module.html#rewrite)，将到根目录的 HTTP 请求导向 HTTPS。

HTTPS server 代码块启用了 ssl 和 http2，还包括您的 SSL 证书和密钥位置，以及您保存在 nginx-conf/options-ssl-nginx.conf 中推荐的 Certbot 安全参数。

此外，包括一些安全头，将使您获得 A 评级的感受，如 [SSL Labs](https://www.ssllabs.com/ssltest/) 和  [Security Headers](https://securityheaders.com/) 服务器测试站点。这些头部包括  [`X-Frame-Options`](https://developer.mozilla.org/en-US/docs/Web/HTTP/Headers/X-Frame-Options), [`X-Content-Type-Options`](https://developer.mozilla.org/en-US/docs/Web/HTTP/Headers/X-Content-Type-Options), [`Referrer Policy`](https://scotthelme.co.uk/a-new-security-header-referrer-policy/), [`Content-Security-Policy`](https://developer.mozilla.org/en-US/docs/Web/HTTP/Headers/Content-Security-Policy), and [`X-XSS-Protection`](https://developer.mozilla.org/en-US/docs/Web/HTTP/Headers/X-XSS-Protection)。

 [HTTP `Strict Transport Security`](https://en.wikipedia.org/wiki/HTTP_Strict_Transport_Security) (HSTS) 头部已注释，如果您明白其含义并且已评估它预加载的功能就可以启用它。

在重建 webserver 服务之前，您需要添加一个 443 映射端口到您 webserver 服务定义中。

打开 docker-compose.yml 文件：

```bash
[root@arm2 wordpress]# vim docker-compose.yml
```

在 webserver 服务定义中添加如下端口映射：

```bash
...
  webserver:
    depends_on:
      - wordpress
    image: nginx:1.22.1-alpine
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
    image: mysql:8.0.31
    container_name: db
    command: '--default-authentication-plugin=mysql_native_password'
    volumes:
      - dbdata:/var/lib/mysql
    restart: unless-stopped
    env_file: .env
    environment:
      - MYSQL_DATABASE=wordpress
    networks:
      - app-network
  wordpress:
    depends_on: 
      - db
    image: wordpress:6.1.0-fpm-alpine
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
    image: nginx:1.22.1-alpine
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
    image: certbot/certbot:arm64v8-v1.32.0
    container_name: certbot
    volumes:
      - certbot-etc:/etc/letsencrypt
      - wordpress:/var/www/html
    command: certonly --webroot --webroot-path=/var/www/html --email brinnatt@gmail.com --agree-tos --no-eff-email --force-renewal -d brinnatt.com -d www.brinnatt.com
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
[root@arm2 wordpress]# docker-compose up -d --force-recreate --no-deps webserver
```

使用 docker-compose ps 查看服务：

```bash
[root@arm2 wordpress]# docker-compose ps -a
NAME                IMAGE                             COMMAND                  SERVICE             CREATED              STATUS                      PORTS
certbot             certbot/certbot:arm64v8-v1.32.0   "certbot certonly --…"   certbot             14 minutes ago       Exited (0) 14 minutes ago   
db                  mysql:8.0.31                      "docker-entrypoint.s…"   db                  25 minutes ago       Up 25 minutes               3306/tcp, 33060/tcp
webserver           nginx:1.22.1-alpine               "/docker-entrypoint.…"   webserver           About a minute ago   Up About a minute           0.0.0.0:80->80/tcp, :::80->80/tcp, 0.0.0.0:443->443/tcp, :::443->443/tcp
wordpress           wordpress:6.1.0-fpm-alpine        "docker-entrypoint.s…"   wordpress           25 minutes ago       Up 25 minutes               9000/tcp
```

接下来可以通过 web 接口来配置 WordPress。

## X8、通过 Web 接口完成安装

容器运行后，通过 WordPress web 界面完成安装，打开浏览器输入 https://www.brinnatt.com。根据提示完成 wordpress 的安装。接着就可以使用 wordpress。

## X9、更新证书

Let's Encrypt 证书的有效期是 90 天。您可以设置一个自动更新流程，以确保它们不会失效。可以设置 cron 任务计划来更新证书并且重载 nginx 配置。

编辑 ssl_renew.sh 脚本文件：

```bash
[root@arm2 wordpress]# vim ssl_renew.sh
```

向脚本中添加以下代码以更新证书并重新加载 web 服务器配置。

```bash
#!/bin/bash

COMPOSE="/usr/local/bin/docker-compose --ansi never"
DOCKER="/usr/bin/docker"

cd /usr/local/wordpress
$COMPOSE run certbot renew --dry-run && $COMPOSE kill -s SIGHUP webserver
$DOCKER system prune -af
```

该脚本首先分配 docker-compose 二进制路径给 COMPOSE 变量，并且指定 `--ansi never(老版本是--no-ansi)` 选项，它将运行没有 [ANSI control characters](https://vt100.net/docs/vt510-rm/chapter4.html) 的 docker-compose 命令。同样地，docker 二进制路径也设置变量。最后进入 `/usr/local/wordpress` 项目根目录并且运行下面 docker-compose 命令：

+ `docker-compose run`：这将启动 certbot 容器并覆盖 certbot 服务定义中提供的命令。这里使用的不是 certonly 子命令，而是 renew 子命令，它将更新即将过期的证书。还包括用于测试脚本的 `--dry-run` 选项。
+ [`docker-compose kill`](https://docs.docker.com/engine/reference/commandline/compose_kill/)：这将向 webserver 容器发送一个 SIGHUP 信号来重新加载 Nginx 配置。

然后执行 [`docker system prune`](https://docs.docker.com/engine/reference/commandline/system_prune/) 来删除所有未使用的容器和镜像。

给该脚本一个执行权限：

```bash
[root@arm2 wordpress]# chmod +x ssl_renew.sh
```

接下来，打开 root crontab 文件，设置时钟计划来运行 ssl_renew.sh 脚本：

```bash
[root@arm2 wordpress]# crontab -e
```

在这个文件的最底部，添加以下一行：

```bash
*/5 * * * * /usr/local/wordpress/ssl_renew.sh >> /var/log/cron.log 2>&1
```

该任务计划设置为每五分钟执行一次，因此您可以测试您的更新请求是否按预期工作。创建一个日志文件 cron.log 来记录任务计划的相关输出。

等待 5 分钟后，检查 cron.log 来确认更新证书请求是否成功。

```bash
[root@arm2 wordpress]# tail -f /var/log/cron.log
```

以下输出确认更新成功：

```bash
Output
- - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -
** DRY RUN: simulating 'certbot renew' close to cert expiry
**          (The test certificates below have not been saved.)

Congratulations, all renewals succeeded. The following certs have been renewed:
  /etc/letsencrypt/live/brinnatt.com/fullchain.pem (success)
** DRY RUN: simulating 'certbot renew' close to cert expiry
**          (The test certificates above have not been saved.)
- - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -
```

> 注意：如果 certbot 容器正在运行，执行该脚本会出现 another container is running 的错误，需要使用 docker-compose 将 certbot 终止。实际上，certbot 的作用就是完成证书相关的任务，一旦完成，certbot 容器就该退出。

成功之后就可以将 `--dry-run` 选项从 ssl_renew.sh 脚本中移除掉：

```bash
#!/bin/bash

COMPOSE="/usr/local/bin/docker-compose --ansi never"
DOCKER="/usr/bin/docker"

cd /usr/local/wordpress
$COMPOSE run certbot renew && $COMPOSE kill -s SIGHUP webserver
$DOCKER system prune -af
```

另外，证书 90 天后才会过期，cron 任务计划没有必要每隔 5 分钟就执行一次，可以把时间间隔设置的更长一些，比如每天执行一次。

```bash
8 8 * * * /usr/local/wordpress/ssl_renew.sh >> /var/log/cron.log 2>&1
```

