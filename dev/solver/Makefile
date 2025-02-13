############################################################################################
#
#  The subroutines below are adapted from the Computational Toolkit provided in: 
#   
#      Schuster, G. T. (2017). Seismic inversion. Society of Exploration Geophysicists.
#
#  We kindly thank Prof. Schuster for allowing us to use these useful and efficient Fortran 
#  subroutines. Please Cite the book above if you use these subroutines.
#
############################################################################################


include ./Makefile.config

# level-1 modules: self-contained
# --level-2 modules: use level-1 modules and are used by level-3 modules
#   --level-3 modules: use both level-1 and level-2 modules

OBJS = $(DIR_OBJ)/global.o    \
       $(DIR_OBJ)/parser.o    \
       $(DIR_OBJ)/datatype.o  \
       $(DIR_OBJ)/mmi_mpi.o   \
       $(DIR_OBJ)/string.o    \
       $(DIR_OBJ)/su.o        \
       $(DIR_OBJ)/io.o        \
       $(DIR_OBJ)/math.o      \
       $(DIR_OBJ)/pml.o       \
       $(DIR_OBJ)/source.o    \
       $(DIR_OBJ)/fdcore.o       \
       $(DIR_OBJ)/fdsolver.o

all: dir fd2dmpi

dir:
	@-test -d $(DIR_MOD) || mkdir $(DIR_MOD)
	@-test -d $(DIR_OBJ) || mkdir $(DIR_OBJ)

fd2dmpi: $(OBJS) $(DIR_OBJ)/fd2dmpi.o
	$(FCC) -o $@ $^ $(LDFLAGS)
	mv $@ ../$(DIR_EXE)

$(DIR_OBJ)/%.o: %.f90
	$(FCC) -o $@ $< $(F90FLAGS)

clean:
	rm -f $(DIR_EXE)/* $(DIR_OBJ)/*.o $(DIR_MOD)/*.mod;