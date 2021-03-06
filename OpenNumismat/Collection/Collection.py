import locale
import os

from PyQt5 import QtCore
from PyQt5.QtWidgets import *
from PyQt5.QtGui import QImage, QPainter, QColor
from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtSql import QSqlTableModel, QSqlDatabase, QSqlQuery, QSqlField

from OpenNumismat.Collection.CollectionFields import CollectionFieldsBase
from OpenNumismat.Collection.CollectionFields import FieldTypes as Type
from OpenNumismat.Collection.CollectionFields import CollectionFields
from OpenNumismat.Collection.CollectionPages import CollectionPages
from OpenNumismat.Collection.Password import cryptPassword, PasswordDialog
from OpenNumismat.Collection.Description import CollectionDescription
from OpenNumismat.Reference.Reference import CrossReferenceSection
from OpenNumismat.Reference.ReferenceDialog import AllReferenceDialog
from OpenNumismat.EditCoinDialog.EditCoinDialog import EditCoinDialog
from OpenNumismat.Collection.CollectionFields import Statuses
from OpenNumismat.Collection.VersionUpdater import updateCollection
from OpenNumismat.Tools.CursorDecorators import waitCursorDecorator
from OpenNumismat.Tools import Gui
from OpenNumismat.Settings import Settings, BaseSettings
from OpenNumismat import version
from OpenNumismat.Collection.Export import ExportDialog
import OpenNumismat

class CollectionModel(QSqlTableModel):
    """Deal with the coins in a collection."""
    rowInserted = pyqtSignal(object)
    modelChanged = pyqtSignal()
    IMAGE_FORMAT = 'jpg'

    def __init__(self, collection, parent=None):
        super(CollectionModel, self).__init__(parent, collection.db)

        self.badFileNameImage = os.path.join(OpenNumismat.PRJ_PATH, 'icons', 'badFileName.png')

        self.intFilter = ''
        self.extFilter = ''

        self.reference = collection.reference
        self.fields = collection.fields
        self.description = collection.description
        self.proxy = None

        self.rowsInserted.connect(self.rowsInsertedEvent)

        self.settings = Settings()

    def rowsInsertedEvent(self, parent, start, end):
        self.insertedRowIndex = self.index(end, 0)

    def data(self, index, role=Qt.DisplayRole):
        if role == Qt.DisplayRole:
            # Localize values
            data = super(CollectionModel, self).data(index, role)
            field = self.fields.fields[index.column()]
            try:
                if field.name == 'status':
                    text = Statuses[data]
                elif field.type == Type.BigInt:
                    text = locale.format("%d", int(data), grouping=True)
                elif field.type == Type.Money:
                    text = locale.format("%.2f", float(data), grouping=True)
                    dp = locale.localeconv()['decimal_point']
                    text = text.rstrip('0').rstrip(dp)
                elif field.type == Type.Value:
                    text = locale.format("%.3f", float(data), grouping=True)
                    dp = locale.localeconv()['decimal_point']
                    text = text.rstrip('0').rstrip(dp)
                elif field.type == Type.Date:
                    date = QtCore.QDate.fromString(data, Qt.ISODate)
                    text = date.toString(Qt.SystemLocaleShortDate)
                elif field.type == Type.Image or field.type == Type.EdgeImage:
                    if data:
                        image, fileName = self.getImage(data)
                        return image
                    else:
                        return None
                elif field.type == Type.PreviewImage:
                    if data:
                        return self.getPreviewImage(data)
                    else:
                        return None
                elif field.type == Type.DateTime:
                    date = QtCore.QDateTime.fromString(data, Qt.ISODate)
                    # Timestamp in DB stored in UTC
                    date.setTimeSpec(Qt.UTC)
                    date = date.toLocalTime()
                    text = date.toString(Qt.SystemLocaleShortDate)
                else:
                    return data
            except (ValueError, TypeError):
                return data
            return text
        elif role == Qt.UserRole:
            return super(CollectionModel, self).data(index, Qt.DisplayRole)
        elif role == Qt.TextAlignmentRole:
            field = self.fields.fields[index.column()]
            if field.type == Type.BigInt:
                return Qt.AlignRight | Qt.AlignVCenter

        return super(CollectionModel, self).data(index, role)

    def dataDisplayRole(self, index):
        return super(CollectionModel, self).data(index, Qt.DisplayRole)

    def addCoin(self, record, parent=None):
        record.setNull('id')  # remove ID value from record
        dialog = EditCoinDialog(self, record, parent)
        result = dialog.exec_()
        if result == QDialog.Accepted:
            self.appendRecord(record)

    def appendRecord(self, record):
        rowCount = self.rowCount()

        self.insertRecord(-1, record)
        self.submitAll()

        if rowCount < self.rowCount():  # inserted row visible in current model
            if self.insertedRowIndex.isValid():
                self.rowInserted.emit(self.insertedRowIndex)

    def insertRecord(self, row, record):
        """Insert a record into the DB after {row}."""
        self._updateRecord(record)
        #krr:todo: A check for an empty DB here would be nice 
        #krr:todo: or checking for duplicate ID
        if not self.settings['id_dates']: #krr:todo: gotta be a better way
            record.setNull('id')  # remove ID value from record
            record.setValue('createdat', record.value('updatedat'))

        for field in ['obverseimg', 'reverseimg', 'edgeimg',
                      'photo1', 'photo2', 'photo3', 'photo4']:
            value = record.value(field)
            if value:
                query = QSqlQuery(self.database())
                query.prepare("INSERT INTO photos (title, image) VALUES (?, ?)")
                query.addBindValue(record.value(field + '_title'))
                query.addBindValue(value)
                query.exec_()
                
                img_id = query.lastInsertId()
            else:
                img_id = None
                
            record.setValue(field, img_id)
            record.remove(record.indexOf(field + '_id'))
            record.remove(record.indexOf(field + '_title'))
            if self.settings['image_name']:
                record.remove(record.indexOf(field + '_orig'))
            
        value = record.value('image')
        if value:
            query = QSqlQuery(self.database())
            query.prepare("INSERT INTO images (image) VALUES (?)")
            query.addBindValue(record.value('image'))
            query.exec_()

            img_id = query.lastInsertId()
        else:
            img_id = None
        record.setValue('image', img_id)
        record.remove(record.indexOf('image_id'))

        return super(CollectionModel, self).insertRecord(row, record)

    def setRecord(self, row, record):
        """Write or delete changes to the coin's info
        (which is held in the "record" variable) to the DB."""
        self._updateRecord(record)
        # TODO : check that images was realy changed
        for field in ['obverseimg', 'reverseimg', 'edgeimg',
                      'photo1', 'photo2', 'photo3', 'photo4']:
            img_id = record.value(field + '_id')
            value = record.value(field)
            if not value:
                #If image has bee removed...
                if img_id:
                    query = QSqlQuery(self.database())
                    query.prepare("DELETE FROM photos WHERE id=?")
                    query.addBindValue(img_id)
                    query.exec_()

                    img_id = None
            else:
                if img_id:
                    #If replaceing an existing image...
                    query = QSqlQuery(self.database())
                    query.prepare("UPDATE photos SET title=?, image=? WHERE id=?")
                    query.addBindValue(record.value(field + '_title'))
                    if self.settings['image_name']:
                        if type(value) is str:
                            query.addBindValue(value)
                        else:
                            if record.value(field + '_orig'):
                                query.addBindValue(record.value(field + '_orig'))
                            else:
                                #While using file names the _orig is null...
                                #when coming from blob in DB, use current value
                                query.addBindValue(value)
                    else:
                        query.addBindValue(value)

                    query.addBindValue(img_id)
                    query.exec_()
                else:
                    #If adding a new image...
                    query = QSqlQuery(self.database())
                    query.prepare("INSERT INTO photos (title, image) VALUES (?, ?)")
                    query.addBindValue(record.value(field + '_title'))
                    query.addBindValue(record.value(field))
                    query.exec_()

                    img_id = query.lastInsertId()

            if img_id:
                record.setValue(field, img_id)
            else:
                record.setNull(field)
            record.remove(record.indexOf(field + '_id'))
            record.remove(record.indexOf(field + '_title'))
            if self.settings['image_name']:
                record.remove(record.indexOf(field + '_orig'))
            
        img_id = record.value('image_id')
        value = record.value('image')
        if not value:
            if img_id:
                query = QSqlQuery(self.database())
                query.prepare("DELETE FROM images WHERE id=?")
                query.addBindValue(img_id)
                query.exec_()
                
                img_id = None
        else:
            if img_id:
                query = QSqlQuery(self.database())
                query.prepare("UPDATE images SET image=? WHERE id=?")
                query.addBindValue(record.value('image'))
                query.addBindValue(img_id)
                query.exec_()
            else:
                query = QSqlQuery(self.database())
                query.prepare("INSERT INTO images (image) VALUES (?)")
                query.addBindValue(record.value('image'))
                query.exec_()
                
                img_id = query.lastInsertId()
                
        if img_id:
            record.setValue('image', img_id)
        else:
            record.setNull('image')
        record.remove(record.indexOf('image_id'))
        
        return super(CollectionModel, self).setRecord(row, record)
    
    def record(self, row=-1):
        """Get a record (row) from the DB."""
        if row >= 0:
            record = super(CollectionModel, self).record(row)
        else:
            record = super(CollectionModel, self).record()

        for field in ['obverseimg', 'reverseimg', 'edgeimg',
                      'photo1', 'photo2', 'photo3', 'photo4']:
            record.append(QSqlField(field + '_title'))
            record.append(QSqlField(field + '_id'))
            if self.settings['image_name']:
                record.append(QSqlField(field + '_orig'))

            img_id = record.value(field)
            if img_id:
                data, fileName = self.getImage(img_id)
                record.setValue(field, data)
                if self.settings['image_name']:
                    record.setValue(field + '_orig', fileName)
                else:
                    record.setValue(field + '_orig', None)
                record.setValue(field + '_title', self.getImageTitle(img_id))
                record.setValue(field + '_id', img_id)
                if self.settings['image_name']:
                    record.setValue(field + '_orig', fileName)
            else:
                record.setValue(field, None)
                fieldDesc = getattr(self.fields, field)
                record.setValue(field + '_title', fieldDesc.title)
                
        record.append(QSqlField('image_id'))
        img_id = record.value('image')
        if img_id:
            data = self.getPreviewImage(img_id)
            record.setValue('image', data)
            record.setValue('image_id', img_id)
        else:
            record.setValue('image', None)
            
        return record
    
    def removeRow(self, row):
        record = super().record(row)
        
        ids = []
        for field in ['obverseimg', 'reverseimg', 'edgeimg',
                      'photo1', 'photo2', 'photo3', 'photo4']:
            value = record.value(field)
            if value:
                ids.append(value)
                
        if ids:
            ids_sql = '(' + ','.join('?' * len(ids)) + ')'
            
            query = QSqlQuery(self.database())
            query.prepare("DELETE FROM photos WHERE id IN " + ids_sql)
            for id_ in ids:
                query.addBindValue(id_)
            query.exec_()
            
        value = record.value('image')
        if value:
            query = QSqlQuery(self.database())
            query.prepare("DELETE FROM images WHERE id=?")
            query.addBindValue(value)
            query.exec_()
            
        return super().removeRow(row)
    
    def _updateRecord(self, record):
        if self.proxy:
            self.proxy.setDynamicSortFilter(False)
            
        obverseImage = QImage()
        reverseImage = QImage()
        for field in self.fields.userFields:
            if field.type in [Type.Image, Type.EdgeImage]:
                # Convert image to DB format
                image = record.value(field.name)
                if isinstance(image, str):
                    # Copying record as text (from Excel) store missed images
                    #    as string
                    # Also, for file names stored in image fields.
                    if image[:7] == "file://":
                        record.setValue(field.name, image)
                    else:
                        record.setNull(field.name)
                elif isinstance(image, QImage):
                    ba = QtCore.QByteArray()
                    buffer = QtCore.QBuffer(ba)
                    buffer.open(QtCore.QIODevice.WriteOnly)
                    
                    # Resize big images for storing in DB
                    sideLen = Settings()['ImageSideLen']
                    sideLen = int(sideLen)  # forced conversion to Integer
                    maxWidth = sideLen
                    maxHeight = sideLen
                    if image.width() > maxWidth or image.height() > maxHeight:
                        scaledImage = image.scaled(maxWidth, maxHeight,
                                Qt.KeepAspectRatio, Qt.SmoothTransformation)
                    else:
                        scaledImage = image
                        
                    if field.name == 'obverseimg':
                        obverseImage = scaledImage
                    if field.name == 'reverseimg':
                        reverseImage = scaledImage
                        
                    scaledImage.save(buffer, self.IMAGE_FORMAT)
                    record.setValue(field.name, ba)
                elif isinstance(image, bytes):
                    ba = QtCore.QByteArray(image)
                    record.setValue(field.name, ba)
                    
        # Creating preview image
        if record.isNull('obverseimg') and record.isNull('reverseimg'):
            record.setNull('image')
        else:
            # Get height of list view for resizing images
            tmp = QTableView()
            height = int(tmp.verticalHeader().defaultSectionSize() * 1.5 - 1)
            
            if not record.isNull('obverseimg') and obverseImage.isNull():
                if record.value('obverseimg')[:7] == 'file://':
                    obverseImage.load(record.value('obverseimg')[7:])
                else:
                    obverseImage.loadFromData(record.value('obverseimg'))
            if not obverseImage.isNull():
                obverseImage = obverseImage.scaledToHeight(height,
                                                    Qt.SmoothTransformation)
            if not record.isNull('reverseimg') and reverseImage.isNull():
                if record.value('reverseimg')[:7] == 'file://':
                    reverseImage.load(record.value('reverseimg')[7:])
                else:
                    reverseImage.loadFromData(record.value('reverseimg'))
            if not reverseImage.isNull():
                reverseImage = reverseImage.scaledToHeight(height,
                                                    Qt.SmoothTransformation)
                
            image = QImage(obverseImage.width() + reverseImage.width(),
                                 height, QImage.Format_RGB32)
            image.fill(QColor(Qt.white).rgb())
            
            paint = QPainter(image)
            if not record.isNull('obverseimg'):
                paint.drawImage(QtCore.QRectF(0, 0, obverseImage.width(), height), obverseImage,
                                QtCore.QRectF(0, 0, obverseImage.width(), height))
            if not record.isNull('reverseimg'):
                paint.drawImage(QtCore.QRectF(obverseImage.width(), 0, reverseImage.width(), height), reverseImage,
                                QtCore.QRectF(0, 0, reverseImage.width(), height))
            paint.end()
            
            ba = QtCore.QByteArray()
            buffer = QtCore.QBuffer(ba)
            buffer.open(QtCore.QIODevice.WriteOnly)
            
            # Store as PNG for better view
            image.save(buffer, 'png')
            record.setValue('image', ba)
            
        if not self.settings['id_dates']: #krr:todo: gotta be a better way
            currentTime = QtCore.QDateTime.currentDateTimeUtc()
            record.setValue('updatedat', currentTime.toString(Qt.ISODate))
            
    def submitAll(self):
        ret = super(CollectionModel, self).submitAll()
        
        if self.proxy:
            self.proxy.setDynamicSortFilter(True)
            
        return ret
    
    def select(self):
        ret = super(CollectionModel, self).select()
        
        self.modelChanged.emit()
        
        return ret
    
    def columnType(self, column):
        if isinstance(column, QtCore.QModelIndex):
            column = column.column()
            
        return self.fields.fields[column].type
    
    def setFilter(self, filter_):
        self.intFilter = filter_
        self.__applyFilter()

    def setAdditionalFilter(self, filter_):
        self.extFilter = filter_
        self.__applyFilter()

    def getImage(self, img_id):
        """Read the image data/fileName from the DB."""
        query = QSqlQuery(self.database())
        query.prepare("SELECT image FROM photos WHERE id=?")
        query.addBindValue(img_id)
        query.exec_()
        if query.first():
            imageOrFile = query.record().value(0)
            if imageOrFile[:7] == 'file://':
                try:
                    imageFile = open(imageOrFile[7:], "rb")
                except IOError:
                    imageFile = open(self.badFileNameImage, "rb")
                image = QtCore.QByteArray(imageFile.read())
                imageFile.close()
                return image, imageOrFile
            else:
                return imageOrFile, None
        
    def getPreviewImage(self, img_id):
        query = QSqlQuery(self.database())
        query.prepare("SELECT image FROM images WHERE id=?")
        query.addBindValue(img_id)
        query.exec_()
        if query.first():
            return query.record().value(0)
        
    def getImageTitle(self, img_id):
        query = QSqlQuery(self.database())
        query.prepare("SELECT title FROM photos WHERE id=?")
        query.addBindValue(img_id)
        query.exec_()
        if query.first():
            return query.record().value(0)
        
    def __applyFilter(self):
        if self.intFilter and self.extFilter:
            combinedFilter = self.intFilter + " AND " + self.extFilter
        else:
            combinedFilter = self.intFilter + self.extFilter
            
        # Checking for SQLITE_MAX_SQL_LENGTH (default value - 1 000 000)
        if len(combinedFilter) > 900000:
            QMessageBox.warning(self.parent(),
                            self.tr("Filtering"),
                            self.tr("Filter is too complex. Will be ignored"))
            return
        
        super(CollectionModel, self).setFilter(combinedFilter)
        
    def isExist(self, record):
        fields = ['title', 'value', 'unit', 'country', 'period', 'year',
                  'mint', 'mintmark', 'type', 'series', 'subjectshort',
                  'status', 'material', 'quality', 'paydate', 'payprice',
                  'seller', 'payplace', 'saledate', 'saleprice', 'buyer',
                  'saleplace', 'variety', 'obversevar', 'reversevar',
                  'edgevar']
        filterParts = [field + '=?' for field in fields]
        sqlFilter = ' AND '.join(filterParts)
        
        db = self.database()
        query = QSqlQuery(db)
        query.prepare("SELECT count(*) FROM coins WHERE id<>? AND " + sqlFilter)
        query.addBindValue(record.value('id'))
        for field in fields:
            query.addBindValue(record.value(field))
        query.exec_()
        if query.first():
            count = query.record().value(0)
            if count > 0:
                return True
            
        return False
    
    
class CollectionSettings(BaseSettings):
    """Setting that pertain to a particular collection. (In a particular DB file.)"""
    Default = {
            'Version': 3,
            'Type': version.AppName,
            'Password': cryptPassword()
    }
    
    def __init__(self, collection):
        super(CollectionSettings, self).__init__()
        self.db = collection.db
        
        if 'settings' not in self.db.tables():
            self.create(self.db)
            
        query = QSqlQuery("SELECT * FROM settings", self.db)
        while query.next():
            record = query.record()
            if record.value('title') in self.keys():
                self.__setitem__(record.value('title'), record.value('value'))
                
    def keys(self):
        return self.Default.keys()
    
    def _getValue(self, key):
        return self.Default[key]
    
    def save(self):
        self.db.transaction()
        
        for key, value in self.items():
            # TODO: Insert value if currently not present
            query = QSqlQuery(self.db)
            query.prepare("UPDATE settings SET value=? WHERE title=?")
            query.addBindValue(str(value))
            query.addBindValue(key)
            query.exec_()

        self.db.commit()
        
    @staticmethod
    def create(db=QSqlDatabase()):
        db.transaction()
        
        sql = """CREATE TABLE settings (
            title CHAR NOT NULL UNIQUE,
            value CHAR)"""
        QSqlQuery(sql, db)
        
        for key, value in CollectionSettings.Default.items():
            query = QSqlQuery(db)
            query.prepare("""INSERT INTO settings (title, value)
                    VALUES (?, ?)""")
            query.addBindValue(key)
            query.addBindValue(str(value))
            query.exec_()
            
        db.commit()
        
        
class Collection(QtCore.QObject):
    """Deal with whole collections. (A particular DB file.)"""
    def __init__(self, reference, parent=None):
        super(Collection, self).__init__(parent)
        
        self.reference = reference
        self.db = QSqlDatabase.addDatabase('QSQLITE')
        self._pages = None
        self.fileName = None
        
    def isOpen(self):
        return self.db.isValid()
    
    def open(self, fileName):
        file = QtCore.QFileInfo(fileName)
        if file.isFile():
            self.db.setDatabaseName(fileName)
            if not self.db.open() or not self.db.tables():
                print(self.db.lastError().text())
                QMessageBox.critical(self.parent(),
                                self.tr("Open collection"),
                                self.tr("Can't open collection %s") % fileName)
                return False
        else:
            QMessageBox.critical(self.parent(),
                                self.tr("Open collection"),
                                self.tr("Collection %s not exists") % fileName)
            return False
        
        self.fileName = fileName
        
        self.settings = CollectionSettings(self)
        if self.settings['Type'] != version.AppName:
            QMessageBox.critical(self.parent(),
                    self.tr("Open collection"),
                    self.tr("Collection %s in wrong format %s") % (fileName, version.AppName))
            return False
        
        if self.settings['Password'] != cryptPassword():
            dialog = PasswordDialog(self, self.parent())
            result = dialog.exec_()
            if result == QDialog.Rejected:
                return False
            
        self.fields = CollectionFields(self.db)
        
        updateCollection(self)
        
        self._pages = CollectionPages(self.db)
        
        self.description = CollectionDescription(self)
        
        return True
    
    def create(self, fileName):
        if QtCore.QFileInfo(fileName).exists():
            QMessageBox.critical(self.parent(),
                                    self.tr("Create collection"),
                                    self.tr("Specified file already exists"))
            return False
        
        self.db.setDatabaseName(fileName)
        if not self.db.open():
            print(self.db.lastError().text())
            QMessageBox.critical(self.parent(),
                                       self.tr("Create collection"),
                                       self.tr("Can't open collection"))
            return False
        
        self.fileName = fileName
        
        self.fields = CollectionFields(self.db)
        
        self.createCoinsTable()
        
        self._pages = CollectionPages(self.db)
        
        self.settings = CollectionSettings(self)
        
        self.description = CollectionDescription(self)
        
        return True
    
    def createCoinsTable(self):
        sqlFields = []
        for field in self.fields:
            if field.name == 'id':
                sqlFields.append('id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT')
            else:
                sqlFields.append("%s %s" % (field.name, Type.toSql(field.type)))
                
        sql = "CREATE TABLE coins (" + ", ".join(sqlFields) + ")"
        QSqlQuery(sql, self.db)
        
        sql = "CREATE TABLE photos (id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT, title TEXT, image BLOB)"
        QSqlQuery(sql, self.db)
        
        sql = "CREATE TABLE images (id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT, image BLOB)"
        QSqlQuery(sql, self.db)
        
    def getFileName(self):
        return QtCore.QDir(self.fileName).absolutePath()
    
    def getCollectionName(self):
        return Collection.fileNameToCollectionName(self.fileName)
    
    def getDescription(self):
        return self.description
    
    def model(self):
        return self.createModel()
    
    def pages(self):
        return self._pages
    
    def createModel(self):
        model = CollectionModel(self)
        model.title = self.getCollectionName()
        model.setEditStrategy(QSqlTableModel.OnManualSubmit)
        model.setTable('coins')
        model.select()
        for field in self.fields:
            model.setHeaderData(field.id, QtCore.Qt.Horizontal, field.title)
            
        return model
    
    def createReference(self):
        sections = self.reference.allSections()
        progressDlg = Gui.ProgressDialog(self.tr("Updating reference"),
                            self.tr("Cancel"), len(sections), self.parent())
        
        for columnName in sections:
            progressDlg.step()
            if progressDlg.wasCanceled():
                break
            
            refSection = self.reference.section(columnName)
            if isinstance(refSection, CrossReferenceSection):
                rel = refSection.model.relationModel(1)
                for i in range(rel.rowCount()):
                    data = rel.data(rel.index(i, rel.fieldIndex('value')))
                    parentId = rel.data(rel.index(i, rel.fieldIndex('id')))
                    query = QSqlQuery(self.db)
                    sql = "SELECT DISTINCT %s FROM coins WHERE %s<>'' AND %s IS NOT NULL AND %s=?" % (columnName, columnName, columnName, refSection.parentName)
                    query.prepare(sql)
                    query.addBindValue(data)
                    query.exec_()
                    refSection.fillFromQuery(parentId, query)
            else:
                sql = "SELECT DISTINCT %s FROM coins WHERE %s<>'' AND %s IS NOT NULL" % (columnName, columnName, columnName)
                query = QSqlQuery(sql, self.db)
                refSection.fillFromQuery(query)
                
        progressDlg.reset()
        
    def editReference(self):
        dialog = AllReferenceDialog(self.reference, self.parent())
        dialog.exec_()
        
    def referenceMenu(self, parent=None):
        createReferenceAct = QAction(self.tr("Fill from collection"), parent)
        createReferenceAct.triggered.connect(self.createReference)
        
        editReferenceAct = QAction(self.tr("Edit..."), parent)
        editReferenceAct.triggered.connect(self.editReference)
        
        return [createReferenceAct, editReferenceAct]
    
    @waitCursorDecorator
    def backup(self):
        backupDir = QtCore.QDir(Settings()['backup'])
        if not backupDir.exists():
            backupDir.mkpath(backupDir.path())
            
        backupFileName = backupDir.filePath("%s_%s.db" % (self.getCollectionName(), QtCore.QDateTime.currentDateTime().toString('yyMMddhhmm')))
        srcFile = QtCore.QFile(self.fileName)
        if not srcFile.copy(backupFileName):
            QMessageBox.critical(self.parent(),
                            self.tr("Backup collection"),
                            self.tr("Can't make a collection backup at %s") %
                                                                backupFileName)
            return False
        
        return True
    
    @waitCursorDecorator
    def vacuum(self):
        QSqlQuery("VACUUM", self.db)
        
    @staticmethod
    def fileNameToCollectionName(fileName):
        file = QtCore.QFileInfo(fileName)
        return file.baseName()
    
    def exportToMobile(self, params):
        SKIPPED_FIELDS = ('edgeimg', 'photo1', 'photo2', 'photo3', 'photo4',
            'obversedesigner', 'reversedesigner', 'catalognum2', 'catalognum3', 'catalognum4',
            'saledate', 'saleprice', 'totalsaleprice', 'buyer', 'saleplace', 'saleinfo',
            'paydate', 'payprice', 'totalpayprice', 'seller', 'payplace', 'payinfo',
            'url', 'obversedesigner', 'reversedesigner')
        
        if os.path.isfile(params['file']):
            os.remove(params['file'])
            
        db = QSqlDatabase.addDatabase('QSQLITE', 'mobile')
        db.setDatabaseName(params['file'])
        if not db.open():
            print(db.lastError().text())
            QMessageBox.critical(self.parent(),
                                       self.tr("Create mobile collection"),
                                       self.tr("Can't open collection"))
            return
        
        mobile_settings = {'Version': 5, 'Type': 'Mobile', 'Filter': params['filter']}
        
        sql = """CREATE TABLE settings (
            title CHAR NOT NULL UNIQUE,
            value CHAR)"""
        QSqlQuery(sql, db)
        for key, value in mobile_settings.items():
            query = QSqlQuery(db)
            query.prepare("""INSERT INTO settings (title, value)
                    VALUES (?, ?)""")
            query.addBindValue(key)
            query.addBindValue(str(value))
            query.exec_()
            
        sql = """CREATE TABLE updates (
            title CHAR NOT NULL UNIQUE,
            value CHAR)"""
        QSqlQuery(sql, db)
        
        sql = """CREATE TABLE photos (
            id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
            image BLOB)"""
        QSqlQuery(sql, db)
        
        sqlFields = []
        fields = CollectionFieldsBase()
        for field in fields:
            if field.name == 'id':
                sqlFields.append('id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT')
            elif field.name == 'image':
                sqlFields.append('image INTEGER')
            elif field.name in SKIPPED_FIELDS:
                continue
            else:
                sqlFields.append("%s %s" % (field.name, Type.toSql(field.type)))
                
        sql = "CREATE TABLE coins (" + ", ".join(sqlFields) + ")"
        QSqlQuery(sql, db)
        
        model = self.model()
        while model.canFetchMore():
            model.fetchMore()
            
        dest_model = QSqlTableModel(self.parent(), db)
        dest_model.setEditStrategy(QSqlTableModel.OnManualSubmit)
        dest_model.setTable('coins')
        dest_model.select()
        
        height = 64
        if params['density'] == 'HDPI':
            height *= 1.5
        elif params['density'] == 'XHDPI':
            height *= 2
        elif params['density'] == 'XXHDPI':
            height *= 3
        elif params['density'] == 'XXXHDPI':
            height *= 4
            
        is_obverse_enabled = params['image'] in (ExportDialog.IMAGE_OBVERSE, ExportDialog.IMAGE_BOTH)
        is_reverse_enabled = params['image'] in (ExportDialog.IMAGE_REVERSE, ExportDialog.IMAGE_BOTH)

        fields = CollectionFieldsBase()
        count = model.rowCount()
        progressDlg = Gui.ProgressDialog(self.tr("Exporting records"),
                                        self.tr("Cancel"), count, self.parent())
        
        for i in range(count):
            progressDlg.step()
            if progressDlg.wasCanceled():
                break
            
            coin = model.record(i)
            if coin.value('status') in ('pass', 'sold'):
                continue
            
            dest_record = dest_model.record()
            
            for field in fields:
                if field.name in ('id', 'image', 'obverseimg', 'reverseimg'):
                    continue
                if field.name in SKIPPED_FIELDS:
                    continue
                
                val = coin.value(field.name)
                if val is None or val == '':
                    continue
                
                dest_record.setValue(field.name, val)
                
            # Process images
            is_obverse_present = not coin.isNull('obverseimg')
            is_reverse_present = not coin.isNull('reverseimg')
            if is_obverse_present or is_reverse_present:
                obverseImage = QImage()
                reverseImage = QImage()
                
                if is_obverse_present and is_obverse_enabled:
                    if coin.value('obverseimg')[:7] == 'file://':
                        obverseImage.load(coin.value('obverseimg')[7:])
                    else:
                        obverseImage.loadFromData(coin.value('obverseimg'))
                    query = QSqlQuery(db)
                    query.prepare("""INSERT INTO photos (image)
                            VALUES (?)""")
                    query.addBindValue(coin.value('obverseimg'))
                    query.exec_()
                    img_id = query.lastInsertId()
                    dest_record.setValue('obverseimg', img_id)
                if not obverseImage.isNull():
                    obverseImage = obverseImage.scaledToHeight(height,
                                                            Qt.SmoothTransformation)
                    
                if is_reverse_present and is_reverse_enabled:
                    if coin.value('reverseimg')[:7] == 'file://':
                        reverseImage.load(coin.value('reverseimg')[7:])
                    else:
                        reverseImage.loadFromData(coin.value('reverseimg'))
                    query = QSqlQuery(db)
                    query.prepare("""INSERT INTO photos (image)
                            VALUES (?)""")
                    query.addBindValue(coin.value('reverseimg'))
                    query.exec_()
                    img_id = query.lastInsertId()
                    dest_record.setValue('reverseimg', img_id)
                if not reverseImage.isNull():
                    reverseImage = reverseImage.scaledToHeight(height,
                                                        Qt.SmoothTransformation)
                    
                if not is_obverse_enabled:
                    obverseImage = QImage()
                if not is_reverse_enabled:
                    reverseImage = QImage()

                image = QImage(obverseImage.width() + reverseImage.width(),
                                     height, QImage.Format_RGB32)
                image.fill(QColor(Qt.white).rgb())
                
                paint = QPainter(image)
                if is_obverse_present and is_obverse_enabled:
                    paint.drawImage(QtCore.QRectF(0, 0, obverseImage.width(), height), obverseImage,
                                    QtCore.QRectF(0, 0, obverseImage.width(), height))
                if is_reverse_present and is_reverse_enabled:
                    paint.drawImage(QtCore.QRectF(obverseImage.width(), 0, reverseImage.width(), height), reverseImage,
                                    QtCore.QRectF(0, 0, reverseImage.width(), height))
                paint.end()
                
                ba = QtCore.QByteArray()
                buffer = QtCore.QBuffer(ba)
                buffer.open(QtCore.QIODevice.WriteOnly)
                
                # Store as PNG for better view
                image.save(buffer, 'png')
                dest_record.setValue('image', ba)
                
            dest_model.insertRecord(-1, dest_record)
            
        progressDlg.setLabelText(self.tr("Saving..."))
        dest_model.submitAll()
        
        progressDlg.setLabelText(self.tr("Compact..."))
        QSqlQuery("""UPDATE coins
SET
  reverseimg = (select t2.id from coins t3 join (select id, image from photos group by image having count(*) > 1) t2 on t1.image = t2.image join photos t1 on t3.reverseimg = t1.id where t1.id <> t2.id and t3.id = coins.id)
WHERE coins.id in (select t3.id from coins t3 join (select id, image from photos group by image having count(*) > 1) t2 on t1.image = t2.image join photos t1 on t3.reverseimg = t1.id where t1.id <> t2.id)
""", db)
        QSqlQuery("""UPDATE coins
SET
  obverseimg = (select t2.id from coins t3 join (select id, image from photos group by image having count(*) > 1) t2 on t1.image = t2.image join photos t1 on t3.obverseimg = t1.id where t1.id <> t2.id and t3.id = coins.id)
WHERE coins.id in (select t3.id from coins t3 join (select id, image from photos group by image having count(*) > 1) t2 on t1.image = t2.image join photos t1 on t3.obverseimg = t1.id where t1.id <> t2.id)
""", db)
        
        QSqlQuery("""DELETE FROM photos
            WHERE id NOT IN (SELECT id FROM photos GROUP BY image)""", db)
        
        progressDlg.setLabelText(self.tr("Vacuum..."))
        QSqlQuery("VACUUM", db)
        
        db.close()
        
        progressDlg.reset()
