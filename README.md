# pdf2tex
Convert PDF documents to tex files using LLM(glm-ocr) without preserving the original format of the PDF.
By running the `pdf2tex_gui.py` program directly, you will see the UI interface. 
（后面有中文介绍）
Step ①: Select a PDF file;
Step ②: Choose an output directory;
Step ③: If the PDF is text-based, select "render" as the processing mode. If the PDF is a scanned version and the largest image on each page is a scanned photo, choose "extract";
Step ④: Enter the start page and end page;
Step ⑤: Enter the API KEY for GLM-OCR from ZhiPu AI. (You need to purchase it yourself, or you can modify the `pdf2tex_gui.py` code to use your own vision large model. I think GLM-OCR works well.)
Step ⑥: Click "Execute All Steps (Step 1->4)".

Wait for the process to complete!

After completion, click "Open Output Directory" to see one `output.tex` file and four folders: `images`, `pdf_pages`, `raw_results`, and `tex_pages`. Their functions are as follows:
- `images` stores PNG images. After the large model performs layout recognition, image regions are marked, and these areas are cropped into separate PNG files by the program.
- `pdf_pages` saves images of each PDF page. If the PDF is a scanned version, this folder stores the largest image from each page.
- `raw_results` stores the original JSON results from the large model’s layout recognition for each PDF page.
- `tex_pages` saves the corresponding LaTeX code for each PDF page.

You can directly compile `output.tex` to generate your new PDF!

If there are errors in the large model’s recognition results, please manually proofread the text and adjust the document format.

# 中文介绍
用大模型将pdf文档转换为tex文件，不保留pdf原本格式。

直接运行pdf2tex_gui.py程序，就可以看到UI界面。

步骤①：选择pdf文件；

步骤②：再选择输出目录；

步骤③：如果pdf是文字版，处理模式选render；如果pdf是扫描版，且每一页pdf中最大的图片是扫描照片，则选择extract；

步骤④：输入起始页和结束页；

步骤⑤：输入智谱glm-ocr的API KEY；（需要自行购买，或者你自己修改pdf2tex_gui.py代码，用你自己的视觉大模型。我觉得glm-ocr不错。）

步骤⑥：点击一键全部执行（Step 1->4）

等待完成！

完成了之后点击“打开输出目录”，可以看到1个output.tex和4个文件夹：images、pdf_pages、raw_results、tex_pages。功能如下：

images保存png图象。大模型版面识别后，标记了图象区域，这些区域被程序截取成为单独的png图片；

pdf_pages保存pdf每一页的图片，如果pdf是扫描版，则此文件夹保存每一页pdf中最大的图片；

raw_results保存每一页pdf经过大模型版面识别之后的原始json结果；

tex_pages保存每一页pdf对应的tex代码。

直接编译output.tex即可得到自己的新pdf！

大模型识别结果有错误，请自己校对文字稿且修改文档格式。
